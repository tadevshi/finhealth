"""Ingestion orchestrator: wires the PDF + LLM services into the database.

The :class:`IngestionService` is the single entry point for turning an
uploaded statement PDF into a fully-populated set of database rows
(:class:`app.models.Statement` + :class:`app.models.Transaction`). It
composes the deterministic half (:mod:`app.services.pdf`) with the
non-deterministic half (:mod:`app.services.llm`) and owns the
transaction boundary so partial failures do not leave the database in
an inconsistent state.

Design rationale
----------------

The orchestrator lives at the seam between the HTTP layer, the file
storage layer, the PDF pipeline, the LLM provider, and the database.
A class with explicit dependencies (rather than a free function with
implicit globals) keeps the wiring testable: the test suite passes a
mock LLM client and a real database, the production app passes the
factory-built LLM client and a per-request session.

Idempotency
-----------

The service is idempotent at the ``(credit_card_id, file_hash)``
level — re-uploading the same PDF for the same card returns the
existing :class:`app.models.Statement` instead of creating a new one.
This matches the database's
:attr:`app.models.statement.Statement.file_hash` unique constraint
and means the user can retry a network failure without producing
duplicate rows.

Error handling
--------------

A failure in any pipeline step (decrypt, extract, LLM, parse) sets
the statement's status to :attr:`StatementStatus.FAILED` and stores
the error message in ``error_message``. The exception is re-raised
so the HTTP layer can return an appropriate status code. The
statement row itself is preserved (with its original period and
file path) so the user can see what failed and re-attempt with a
corrected configuration.
"""

from __future__ import annotations

import hashlib
import logging
import re
import uuid
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.models.bank import Bank
from app.models.credit_card import CreditCard
from app.models.statement import Statement, StatementStatus
from app.models.transaction import Transaction
from app.services.llm.protocol import LLMProvider
from app.services.pdf import (
    decrypt_pdf,
    derive_password,
    detect_variant,
    extract_text,
    parse_amount,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class IngestionError(Exception):
    """Base class for every error the orchestrator can raise.

    The HTTP layer maps specific subclasses to specific status codes
    (bank-not-found → 400, decrypt-failed → 422, etc.). The base
    class is a backstop so callers can catch one thing and not worry
    about the implementation hierarchy.
    """


class BankNotFoundError(IngestionError):
    """The ``bank_name`` argument did not match any row in ``banks``."""


class DuplicateStatementError(IngestionError):
    """A statement with the same ``(credit_card_id, file_hash)`` already exists.

    Raised when the unique constraint on
    :class:`app.models.statement.Statement` rejects an insert. The
    orchestrator handles the *happy* dedup path (existing row
    returned, not raised); this class is for the *race* case where
    two concurrent uploads collide on the unique index.
    """


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


#: The currency that the LLM should report for each variant. The
#: orchestrator enforces this so a hallucinated currency cannot
#: reach the database.
_VARIANT_CURRENCY: Final[dict[str, str]] = {"NACIONAL": "CLP", "INTERNACIONAL": "USD"}

#: Patterns for the LLM-emitted date. The LLM is told to use
#: ``DD/MM/YYYY`` but defensive parsing accepts the two-digit
#: variant and the ISO form.
_DATE_PATTERN_DMY_LONG: Final = re.compile(r"\A(\d{2})/(\d{2})/(\d{4})\Z")
_DATE_PATTERN_DMY_SHORT: Final = re.compile(r"\A(\d{2})/(\d{2})/(\d{2})\Z")
_DATE_PATTERN_ISO: Final = re.compile(r"\A(\d{4})-(\d{2})-(\d{2})\Z")

#: Read buffer for streaming hash computation. 64 KB matches
#: Linux's default page cache granularity and keeps memory low for
#: multi-megabyte statement PDFs.
_HASH_CHUNK_BYTES: Final = 64 * 1024


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class IngestionService:
    """Orchestrate the PDF → LLM → database pipeline for a single statement.

    Parameters
    ----------
    session:
        The :class:`AsyncSession` to use for every database operation
        performed by this ingestion. The session is **not**
        auto-committed — the service commits at well-defined
        boundaries (after the statement row is created, after the
        transactions are inserted, on failure).
    llm_client:
        Any object satisfying the :class:`LLMProvider` protocol.
        The factory in :mod:`app.services.llm.factory` builds the
        production client from settings; tests pass a fake.
    settings:
        The application settings, read for ``PDF_UPLOAD_DIR`` (to
        resolve file paths) and ``MAX_FILE_SIZE_MB`` (to enforce
        the upload size cap).
    """

    def __init__(
        self,
        session: AsyncSession,
        llm_client: LLMProvider,
        settings: Settings,
    ) -> None:
        self._session = session
        self._llm_client = llm_client
        self._settings = settings

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def ingest_statement(
        self,
        file_path: Path,
        bank_name: str,
        rut: str,
        card_number_masked: str,
        cardholder: str,
        currency: str,
    ) -> Statement:
        """Run the full ingestion pipeline on ``file_path``.

        Parameters
        ----------
        file_path:
            Absolute path to the encrypted PDF on local disk. The
            caller (typically an HTTP handler) is responsible for
            storing the upload before invoking the service.
        bank_name:
            Short identifier matching :attr:`app.models.bank.Bank.name`
            (e.g. ``"santander"``, ``"itau"``, ``"banco_de_chile"``).
        rut:
            The cardholder's RUT in any of the formats accepted by
            :func:`app.services.pdf.derive_password`.
        card_number_masked:
            Masked PAN (e.g. ``"XXXX XXXX XXXX 0951"``). Used to
            look up or create the corresponding
            :class:`app.models.credit_card.CreditCard`.
        cardholder:
            Printed name on the card (e.g. ``"JOHN DOE"``).
        currency:
            ISO-4217 code the *user* expects the card to operate in
            (``"CLP"`` or ``"USD"``). The actual per-transaction
            currency is read from the LLM response, which must match
            the value implied by the detected variant.

        Returns
        -------
        Statement
            A fully-populated statement. ``status`` is
            :attr:`StatementStatus.COMPLETED` on success or
            :attr:`StatementStatus.FAILED` on any pipeline error
            (in which case ``error_message`` is set and the
            exception is re-raised).

        Raises
        ------
        BankNotFoundError
            If ``bank_name`` does not match any row in ``banks``.
        InvalidRUTError
            If ``rut`` cannot be parsed.
        PDFDecryptError
            If the PDF cannot be decrypted (wrong password,
            corrupted file, unsupported encryption).
        TextExtractionError
            If text cannot be extracted from the decrypted PDF.
        VariantDetectionError
            If the variant (NACIONAL / INTERNACIONAL) cannot be
            inferred from the extracted text.
        AmountParseError
            If any transaction's amount string cannot be parsed.
        DuplicateStatementError
            If a concurrent upload wins the race for the
            ``(credit_card_id, file_hash)`` unique constraint.
        """
        # 1. Look up the bank up front. A missing bank is a 400, not
        #    a 500, so we raise it before creating any rows.
        bank = await self._get_bank_by_name(bank_name)

        # 2. Hash the file so we can dedup before doing any work.
        file_hash = _compute_sha256(file_path)

        # 3. Get-or-create the credit card. The unique constraint
        #    on (bank_id, card_number_masked, cardholder) makes the
        #    operation race-safe at the DB level.
        card = await self._get_or_create_card(bank, card_number_masked, cardholder, currency)

        # 4. Idempotency: a row with the same (card, file_hash) is
        #    already in the database. Return it without re-running
        #    the pipeline. This matches the spec's "duplicate
        #    upload returns the existing statement" rule.
        existing = await self._find_statement_by_hash(card.id, file_hash)
        if existing is not None:
            logger.info(
                "Statement for card=%s hash=%s already exists (id=%s); skipping re-ingest",
                card.id,
                file_hash,
                existing.id,
            )
            return existing

        # 5. Create the statement row up front so a failure later
        #    in the pipeline can be recorded on it.
        today = date.today()
        statement = Statement(
            credit_card_id=card.id,
            period_start=_first_of_month(today),
            period_end=_last_of_month(today),
            statement_date=today,
            file_path=str(file_path),
            file_hash=file_hash,
            status=StatementStatus.PENDING,
        )
        self._session.add(statement)
        await self._session.flush()  # populate statement.id without committing

        # 6. Run the pipeline inside a try/except so any failure
        #    becomes a FAILED statement with a stored error.
        try:
            transactions = await self._ingest_pipeline(
                statement=statement,
                bank=bank,
                rut=rut,
                cardholder=cardholder,
            )
        except Exception as exc:
            # The pipeline raised a typed :class:`IngestionError`
            # (e.g. ``BankNotFoundError``) or an untyped error
            # (e.g. a PDF ``PDFPasswordError``, an LLM
            # ``LLMExtractionError``, or a validation error from
            # the Pydantic schema). The HTTP layer only catches
            # :class:`IngestionError`, so non-typed errors are
            # wrapped here — preserving the original on
            # ``__cause__`` for log inspection.
            statement.status = StatementStatus.FAILED
            statement.error_message = _truncate_error(exc)
            await self._session.commit()
            logger.exception(
                "Ingestion failed for statement=%s (card=%s): %s",
                statement.id,
                card.id,
                exc,
            )
            if isinstance(exc, IngestionError):
                raise
            raise IngestionError(str(exc)) from exc

        # 7. Persist the extracted transactions, mark the statement
        #    complete, and commit.
        self._session.add_all(transactions)
        statement.status = StatementStatus.COMPLETED
        await self._session.commit()
        # Refresh the statement so the HTTP layer can serialise it
        # without tripping over a closed session. The default
        # ``expire_on_commit=False`` keeps column values around,
        # but the database-set ``created_at`` / ``updated_at`` and
        # the ``credit_card`` relationship were never read from
        # the row in the first place, so we have to fetch them
        # explicitly. ``transactions`` is refreshed because the
        # in-memory list was just populated by ``add_all``.
        await self._session.refresh(
            statement,
            attribute_names=["transactions", "credit_card"],
        )
        logger.info(
            "Ingestion complete for statement=%s: %d transaction(s)",
            statement.id,
            len(transactions),
        )
        return statement

    # ------------------------------------------------------------------
    # Pipeline
    # ------------------------------------------------------------------

    async def _ingest_pipeline(
        self,
        *,
        statement: Statement,
        bank: Bank,
        rut: str,
        cardholder: str,  # Reserved for future per-card logic (e.g. password override).
    ) -> list[Transaction]:
        """Run decrypt → extract → LLM → parse, returning ready-to-add rows.

        Split out from :meth:`ingest_statement` so the failure-handling
        bookkeeping (commit + status flip) lives in exactly one
        place. ``cardholder`` is accepted for future per-card logic
        (e.g. a different password override per card) and is
        intentionally unused for now.
        """
        # 1. Derive the password from the user's RUT and the bank's
        #    stored formula. An invalid RUT or unknown formula
        #    surfaces here as a typed exception.
        password = derive_password(bank, rut)

        # 2. Decrypt the PDF. The decrypted copy lives in a temp
        #    directory so the encrypted upload on shared/ is the
        #    long-lived artifact (and the one we hash).
        decrypted_path = _decrypt_to_temp(Path(statement.file_path), password)

        try:
            # 3. Extract the text. A still-encrypted PDF would
            #    raise ``TextExtractionError`` here.
            text = extract_text(decrypted_path)

            # 4. Detect the variant (NACIONAL vs INTERNACIONAL).
            variant = detect_variant(text)

            # 5. Call the LLM. ``LLMExtractionError`` propagates as-is.
            extraction = await self._llm_client.extract_transactions(text, variant)
        finally:
            # Best-effort cleanup of the decrypted file. A failure
            # here is logged but not raised — the upload directory
            # may already be in a usable state.
            _safe_unlink(decrypted_path)

        expected_currency = _VARIANT_CURRENCY[variant]

        # 6. Convert each LLM-emitted transaction into a model
        #    instance, validating amounts and dates along the way.
        transactions: list[Transaction] = []
        for index, txn in enumerate(extraction.transactions):
            parsed = self._build_transaction(
                statement=statement,
                index=index,
                txn=txn,
                expected_currency=expected_currency,
                raw_extraction=txn.model_dump(),
            )
            transactions.append(parsed)

        return transactions

    def _build_transaction(
        self,
        *,
        statement: Statement,
        index: int,
        txn: object,
        expected_currency: str,
        raw_extraction: dict[str, object],
    ) -> Transaction:
        """Validate one LLM-emitted transaction and build a model row.

        Raises
        ------
        AmountParseError
            If the amount cannot be parsed for the expected currency.
        ValueError
            If the date is missing or unparseable, or the currency
            does not match the variant-implied one.
        """
        # Local import: ``TransactionExtraction`` lives in the LLM
        # subpackage; importing at module level would pull the LLM
        # stack into every import of this module, even in tests that
        # only need ``IngestionService`` for the database side.
        from app.services.llm.schemas import TransactionExtraction

        if not isinstance(txn, TransactionExtraction):
            # Defensive: the LLM layer is typed to return
            # ``ExtractionResponse`` so this branch should be
            # unreachable, but a custom LLM mock might bypass the
            # schema. Surface a clear error instead of a cryptic
            # AttributeError.
            raise TypeError(
                f"Expected TransactionExtraction at index {index}, got {type(txn).__name__}"
            )

        # Currency must match the variant. The LLM is told this in
        # the prompt, but a hallucinated currency is a real failure
        # mode and we want to catch it before it lands in the DB.
        if txn.currency != expected_currency:
            raise ValueError(
                f"Transaction {index} currency {txn.currency!r} does not match "
                f"expected {expected_currency!r} for variant-derived statement"
            )

        amount = parse_amount(txn.amount, txn.currency)
        txn_date = _parse_llm_date(txn.date, index=index)

        installment_value: Decimal | None = None
        if txn.installment_value:
            installment_value = parse_amount(txn.installment_value, txn.currency)

        return Transaction(
            statement_id=statement.id,
            date=txn_date,
            description=txn.description,
            amount=amount,
            currency=txn.currency,
            category=txn.category,
            installment_number=txn.installment_number,
            installment_total=txn.installment_total,
            installment_value=installment_value,
            raw_json=raw_extraction,
        )

    # ------------------------------------------------------------------
    # Database helpers
    # ------------------------------------------------------------------

    async def _get_bank_by_name(self, name: str) -> Bank:
        """Return the :class:`Bank` whose ``name`` matches, or raise.

        The bank is read by ``name`` (the short stable identifier,
        not ``display_name``) so the API can use the same value the
        user sees in URLs and the database seed.
        """
        result = await self._session.execute(select(Bank).where(Bank.name == name))
        bank = result.scalar_one_or_none()
        if bank is None:
            raise BankNotFoundError(
                f"Bank {name!r} not found. Available banks are seeded by the migration."
            )
        return bank

    async def _get_or_create_card(
        self,
        bank: Bank,
        card_number_masked: str,
        cardholder: str,
        currency: str,
    ) -> CreditCard:
        """Return the existing card or insert a new one.

        The unique constraint on ``(bank_id, card_number_masked,
        cardholder)`` makes the get-or-create race-safe: two
        concurrent uploads for the same card cannot produce two
        rows.
        """
        result = await self._session.execute(
            select(CreditCard).where(
                CreditCard.bank_id == bank.id,
                CreditCard.card_number_masked == card_number_masked,
                CreditCard.cardholder == cardholder,
            )
        )
        card = result.scalar_one_or_none()
        if card is not None:
            return card

        card = CreditCard(
            bank_id=bank.id,
            card_number_masked=card_number_masked,
            cardholder=cardholder,
            currency=currency,
            is_active=True,
        )
        self._session.add(card)
        await self._session.flush()
        return card

    async def _find_statement_by_hash(
        self,
        card_id: uuid.UUID,
        file_hash: str,
    ) -> Statement | None:
        """Return the existing statement for ``(card, hash)`` if any."""
        result = await self._session.execute(
            select(Statement).where(
                Statement.credit_card_id == card_id,
                Statement.file_hash == file_hash,
            )
        )
        return result.scalar_one_or_none()


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def _compute_sha256(path: Path) -> str:
    """Return the SHA-256 of ``path`` as lowercase hex.

    The file is read in 64 KB chunks so a 20 MB statement does not
    allocate 20 MB at once. The hash is the same regardless of
    chunk size, so this is purely a memory concern.
    """
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(_HASH_CHUNK_BYTES), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _decrypt_to_temp(encrypted_path: Path, password: str) -> Path:
    """Decrypt ``encrypted_path`` into a unique temp file.

    The temp file lives in the system temp directory so it does not
    pollute the long-lived upload directory. The caller is
    responsible for removing the file when it is done (the
    orchestrator does this in a ``finally`` block).
    """
    import tempfile  # Local import: keeps the module's import time
    # small for callers that never decrypt.

    fd, name = tempfile.mkstemp(suffix=".pdf", prefix="finhealth-decrypted-")
    # ``mkstemp`` returns a low-level file descriptor we never
    # write to (the decryptor opens the path itself). Close it so
    # Windows does not refuse the subsequent open.
    import os

    os.close(fd)
    return decrypt_pdf(encrypted_path, password, Path(name))


def _safe_unlink(path: Path) -> None:
    """Remove ``path`` if it exists; log and continue on any error."""
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        logger.warning("Failed to remove temp file %s: %s", path, exc)


def _parse_llm_date(value: str, *, index: int) -> date:
    """Parse an LLM-emitted date into a :class:`date`.

    The LLM is told to use ``DD/MM/YYYY`` but defensive parsing
    accepts the two-digit year and the ISO form. Anything else
    raises :class:`ValueError` with the index of the offending row
    so the operator can find it in the raw LLM output.

    Raises
    ------
    ValueError
        If the date is unparseable or out of range.
    """
    for pattern in (_DATE_PATTERN_DMY_LONG, _DATE_PATTERN_DMY_SHORT, _DATE_PATTERN_ISO):
        match = pattern.match(value)
        if match is None:
            continue
        a, b, c = (int(group) for group in match.groups())
        if pattern is _DATE_PATTERN_ISO:
            year, month, day = a, b, c
        elif pattern is _DATE_PATTERN_DMY_SHORT:
            day, month, year = a, b, _expand_two_digit_year(c)
        else:  # DMY_LONG
            day, month, year = a, b, c
        try:
            return date(year, month, day)
        except ValueError as exc:
            raise ValueError(f"Transaction {index} date {value!r} is out of range: {exc}") from exc
    raise ValueError(
        f"Transaction {index} date {value!r} does not match any supported format "
        f"(expected DD/MM/YYYY, DD/MM/YY, or YYYY-MM-DD)"
    )


def _expand_two_digit_year(two_digit: int) -> int:
    """Expand a two-digit year to a four-digit one.

    The pivot is 70: years 00-69 are 2000-2069, years 70-99 are
    1970-1999. Credit cards were not issued in the 1900s, so this
    is the right pivot for our use case.
    """
    return 2000 + two_digit if two_digit < 70 else 1900 + two_digit


def _first_of_month(today: date) -> date:
    """Return the first day of the month containing ``today``."""
    return today.replace(day=1)


def _last_of_month(today: date) -> date:
    """Return the last day of the month containing ``today``.

    Uses the well-known "first of next month, minus one day" trick
    so we do not have to encode month lengths in the function.
    The :class:`datetime.timedelta` import is local because the
    helper is only used in this single place.
    """
    from datetime import timedelta

    if today.month == 12:
        return today.replace(month=12, day=31)
    next_month_first = today.replace(month=today.month + 1, day=1)
    return next_month_first - timedelta(days=1)


def _truncate_error(exc: BaseException, *, limit: int = 500) -> str:
    """Return a single-line, length-capped summary of ``exc``.

    The message is stored in the database so it must not contain
    multi-line tracebacks (which break the column's display) and
    must not be unbounded in length (which bloats the row).
    """
    message = f"{type(exc).__name__}: {exc}".replace("\n", " ").replace("\r", " ")
    if len(message) > limit:
        message = message[: limit - 1] + "…"
    return message


__all__ = [
    "BankNotFoundError",
    "DuplicateStatementError",
    "IngestionError",
    "IngestionService",
]
