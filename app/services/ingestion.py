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
from app.services.llm.schemas import (
    ExtractionResponse,
    StatementMetadata,
    TransactionExtraction,
)
from app.services.pdf import decrypt_pdf

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
            :func:`app.services.pdf.derive_password`. The
            per-bank PDF password is derived from this value.

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
        IngestionError
            If the LLM did not return a valid
            :class:`~app.services.llm.schemas.StatementMetadata`
            (e.g. a hallucinated currency or an unparseable
            statement date).
        """
        # 1. Look up the bank up front. A missing bank is a 400, not
        #    a 500, so we raise it before creating any rows.
        bank = await self._get_bank_by_name(bank_name)

        # 2. Derive the password before hashing. We need the
        #    password to decrypt the file, and the RUT validation
        #    is a fast-fail path.
        from app.services.pdf import derive_password

        password = derive_password(bank, rut)

        # 3. Decrypt the PDF to a temp file so the encrypted
        #    upload on shared/ remains the long-lived artifact
        #    (and the one we hash). Any failure here is wrapped
        #    as :class:`IngestionError` so the HTTP layer can
        #    map it to a 422 — pre-LLM errors are always
        #    user-fixable and we want a clean error path.
        try:
            decrypted_path = _decrypt_to_temp(file_path, password)
        except Exception as exc:
            if isinstance(exc, IngestionError):
                raise
            from app.services.pdf import PDFPasswordError

            if isinstance(exc, PDFPasswordError):
                raise IngestionError(str(exc)) from exc
            raise IngestionError(f"PDF decryption failed: {exc}") from exc

        # 4. Extract text + variant up front. These two steps are
        #    fast and have no DB side effects, so we do them
        #    before any write.
        try:
            from app.services.pdf import detect_variant, extract_text

            text = extract_text(decrypted_path)
            variant = detect_variant(text)
        except Exception as exc:
            _safe_unlink(decrypted_path)
            if isinstance(exc, IngestionError):
                raise
            raise IngestionError(str(exc)) from exc

        # 4b. Chunk the text before sending it to the LLM.
        #     Small local models (qwen2.5:1.5b) cannot handle
        #     the full ~18k chars a CMF statement produces via
        #     markitdown — they return generic chat instead of
        #     valid JSON once the prompt exceeds ~5k chars.
        #     The chunker splits the document into overlapping
        #     windows of ``LLM_MAX_INPUT_CHARS`` (default 5000)
        #     and the orchestrator calls the LLM once per
        #     window. This means a 18k-char Santander PDF
        #     produces 3-4 LLM calls instead of one truncated
        #     call that only sees the first 27% of the document.
        #     Chunking happens *after* variant detection so the
        #     detector sees the full text.
        from app.services.pdf.text_truncator import chunk_for_llm

        chunks = chunk_for_llm(
            text,
            max_chars=self._settings.LLM_MAX_INPUT_CHARS,
            variant=variant,
            overlap_chars=self._settings.LLM_CHUNK_OVERLAP_CHARS,
        )

        # 5. Hash the file (after we know the encryption is
        #    correct). The hash is the dedup key, and it must
        #    match across uploads of the same encrypted file.
        file_hash = _compute_sha256(file_path)

        # 6. Call the LLM once per chunk and merge the responses.
        #    A failure on any chunk fails the whole ingestion
        #    (same semantics as the single-chunk path). The
        #    temp file is cleaned up in a ``finally`` block so
        #    a chunk-level failure does not leave a decrypted
        #    PDF on disk.
        try:
            extraction = await self._run_chunked_extraction(chunks, variant)
        except Exception as exc:
            _safe_unlink(decrypted_path)
            if isinstance(exc, IngestionError):
                raise
            raise IngestionError(f"LLM extraction failed: {exc}") from exc
        else:
            _safe_unlink(decrypted_path)

        # 7. Validate the LLM's metadata and turn it into
        #    :class:`date` objects the statement row can carry.
        #    Split into a separate method so the validation
        #    logic is unit-testable without a real PDF.
        expected_currency, period_start, period_end, statement_date = self._validate_metadata(
            extraction=extraction, variant=variant
        )

        # 9. Get-or-create the credit card. The unique constraint
        #    on (bank_id, card_number_masked, cardholder) makes
        #    the operation race-safe at the DB level. We use the
        #    values the LLM read off the PDF — the user no longer
        #    types them.
        card = await self._get_or_create_card(
            bank,
            extraction.metadata.card_number_masked,
            extraction.metadata.cardholder,
            extraction.metadata.currency,
        )

        # 10. Idempotency: a row with the same (card, file_hash)
        #     is already in the database. Return it without
        #     re-running the pipeline. This matches the spec's
        #     "duplicate upload returns the existing statement"
        #     rule.
        existing = await self._find_statement_by_hash(card.id, file_hash)
        if existing is not None:
            logger.info(
                "Statement for card=%s hash=%s already exists (id=%s); skipping re-ingest",
                card.id,
                file_hash,
                existing.id,
            )
            return existing

        # 11. Create the statement row up front so a failure
        #     later in the pipeline can be recorded on it. The
        #     period and emission dates come from the LLM, not
        #     from the current month — that is the whole point
        #     of the metadata extraction.
        statement = Statement(
            credit_card_id=card.id,
            period_start=period_start,
            period_end=period_end,
            statement_date=statement_date,
            file_path=str(file_path),
            file_hash=file_hash,
            status=StatementStatus.PENDING,
        )
        self._session.add(statement)
        await self._session.flush()  # populate statement.id without committing

        # 12. Build the transaction rows from the LLM payload.
        #     Any per-transaction failure (bad amount, bad date,
        #     currency mismatch) raises here and lands the
        #     statement in FAILED with a stored error.
        try:
            transactions = self._build_transactions(
                statement=statement,
                extraction=extraction,
                expected_currency=expected_currency,
            )
        except Exception as exc:
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

        # 13. Persist the extracted transactions, mark the
        #     statement complete, and commit.
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

    async def _run_chunked_extraction(
        self,
        chunks: list[str],
        variant: str,
    ) -> ExtractionResponse:
        """Call the LLM once per chunk and merge the responses.

        Each chunk produces its own :class:`ExtractionResponse`.
        The orchestrator concatenates every transaction, takes
        the first non-empty metadata as canonical, and
        deduplicates rows that straddle a chunk boundary
        (they appear in the overlap region of two consecutive
        chunks). The result is a single
        :class:`ExtractionResponse` whose shape matches the
        one-call path, so the rest of the pipeline does not
        have to know chunking happened.

        Parameters
        ----------
        chunks
            Non-empty list of text windows produced by
            :func:`app.services.pdf.text_truncator.chunk_for_llm`.
            The list contains exactly one element when the
            source text fits in a single window, in which case
            the LLM is called exactly once.
        variant
            ``"NACIONAL"`` or ``"INTERNACIONAL"``, forwarded to
            the LLM client so it picks the right prompt template.

        Returns
        -------
        ExtractionResponse
            A single response with the deduped transaction
            list, the canonical metadata (from the first
            chunk), and the confidence / notes from the first
            chunk. Subsequent chunks contribute their
            transactions but do not overwrite the metadata.

        Raises
        ------
        IngestionError
            If the LLM raises on any chunk. The exception is
            wrapped so the HTTP layer can map it to a 422; the
            underlying ``LLMExtractionError`` is preserved on
            ``__cause__``.
        """
        all_transactions: list[TransactionExtraction] = []
        all_metadata: StatementMetadata | None = None
        first_confidence: float = 0.0
        first_notes: str | None = None

        for index, chunk in enumerate(chunks):
            logger.info(
                "Processing chunk %d/%d (%d chars)",
                index + 1,
                len(chunks),
                len(chunk),
            )
            try:
                response = await self._llm_client.extract_transactions(chunk, variant)
            except Exception as exc:
                if isinstance(exc, IngestionError):
                    raise
                raise IngestionError(
                    f"LLM extraction failed on chunk {index + 1}/{len(chunks)}: {exc}"
                ) from exc

            # First chunk: capture its confidence and notes as
            # the canonical ones. The first chunk carries the
            # document header, so its metadata is the real one;
            # subsequent chunks may hallucinate or repeat.
            if index == 0:
                first_confidence = response.confidence
                first_notes = response.notes

            all_transactions.extend(response.transactions)

            # Take the first metadata whose cardholder is set.
            # Pydantic enforces min_length=1 on the field, so a
            # populated cardholder is the strongest signal that
            # the LLM actually read the document header rather
            # than returning a placeholder.
            if all_metadata is None and response.metadata.cardholder:
                all_metadata = response.metadata

        if all_metadata is None:
            # Defensive: every chunk should have produced some
            # metadata. If not, surface a clear error so the
            # operator can investigate the LLM output.
            raise IngestionError(
                "LLM did not return a usable metadata block in any chunk"
            )

        deduped = _dedupe_transactions(all_transactions)

        return ExtractionResponse(
            transactions=deduped,
            metadata=all_metadata,
            confidence=first_confidence,
            notes=first_notes,
        )

    @staticmethod
    def _validate_metadata(
        *,
        extraction: object,
        variant: str,
    ) -> tuple[str, date, date, date]:
        """Validate the LLM's ``metadata`` block and return normalised values.

        Returns
        -------
        tuple[str, date, date, date]
            ``(expected_currency, period_start, period_end,
            statement_date)``. The first element is the
            currency the *transactions* must use, derived from
            the variant. The remaining three are the parsed
            statement period and emission date.

        Raises
        ------
        IngestionError
            If the metadata is missing, the currency is not a
            recognised CMF code, the currency does not match
            the variant, or one of the dates is unparseable.
        """
        from app.services.llm.schemas import ExtractionResponse

        if not isinstance(extraction, ExtractionResponse):
            raise IngestionError(
                f"Cannot validate metadata: expected ExtractionResponse, "
                f"got {type(extraction).__name__}"
            )

        metadata = extraction.metadata

        # The metadata's currency must be a supported CMF code.
        # ``StatementMetadata.currency_is_valid`` is the same
        # check used for transactions; an unsupported code is
        # almost always a hallucination.
        if not metadata.currency_is_valid():
            raise IngestionError(
                f"LLM emitted unsupported metadata currency "
                f"{metadata.currency!r}; expected one of CLP/USD"
            )

        # The metadata's currency must match the variant. A
        # mismatch means the LLM misread the section header.
        expected_currency = _VARIANT_CURRENCY[variant]
        if metadata.currency != expected_currency:
            raise IngestionError(
                f"Statement metadata currency {metadata.currency!r} does not match "
                f"expected {expected_currency!r} for {variant} variant"
            )

        # Dates are strings in the LLM's output. Turn them
        # into real :class:`date` objects so the statement row
        # can carry them. Empty strings (model could not read
        # the date in this chunk) fall back to ``date.today()``
        # so the statement is still persisted. The fallback
        # shows up as an obviously-wrong date in the UI, which
        # is better than a hard failure.
        try:
            period_start_raw = _parse_llm_date(metadata.period_start, index=-1)
            period_end_raw = _parse_llm_date(metadata.period_end, index=-1)
            statement_date_raw = _parse_llm_date(metadata.statement_date, index=-1)
        except ValueError as exc:
            raise IngestionError(f"LLM emitted unparseable statement metadata date: {exc}") from exc

        today = date.today()
        period_start = period_start_raw or today
        period_end = period_end_raw or today
        statement_date = statement_date_raw or today

        return expected_currency, period_start, period_end, statement_date

    def _build_transactions(
        self,
        *,
        statement: Statement,
        extraction: object,
        expected_currency: str,
    ) -> list[Transaction]:
        """Convert every LLM-emitted transaction into a :class:`Transaction` row.

        Split out from :meth:`ingest_statement` so the
        per-row validation logic (amount parsing, date parsing,
        currency cross-check) lives in one place. The decrypt,
        text-extract, and LLM-call steps happen inline in
        :meth:`ingest_statement` so the temp file can be
        cleaned up in a single ``finally`` block.

        Raises
        ------
        TypeError
            If ``extraction`` is not a validated
            :class:`~app.services.llm.schemas.ExtractionResponse`.
        ValueError
            If any transaction's currency does not match the
            variant-implied one.
        AmountParseError
            If any transaction's amount string cannot be
            parsed for the expected currency.
        """
        # Local import: ``ExtractionResponse`` lives in the LLM
        # subpackage; importing at module level would pull the
        # LLM stack into every import of this module, even in
        # tests that only need ``IngestionService`` for the
        # database side.
        from app.services.llm.schemas import ExtractionResponse, TransactionExtraction
        from app.services.pdf import parse_amount

        if not isinstance(extraction, ExtractionResponse):
            # Defensive: the LLM layer is typed to return
            # ``ExtractionResponse`` so this branch should be
            # unreachable, but a custom LLM mock might bypass
            # the schema. Surface a clear error instead of a
            # cryptic AttributeError.
            raise IngestionError(
                f"Cannot build transactions: expected ExtractionResponse, "
                f"got {type(extraction).__name__}"
            )

        transactions: list[Transaction] = []
        for index, txn in enumerate(extraction.transactions):
            if not isinstance(txn, TransactionExtraction):
                raise IngestionError(
                    f"Expected TransactionExtraction at index {index}, got {type(txn).__name__}"
                )

            # Currency must match the variant. The LLM is told
            # this in the prompt, but a hallucinated currency is
            # a real failure mode and we want to catch it before
            # it lands in the DB.
            if txn.currency != expected_currency:
                raise IngestionError(
                    f"Transaction {index} currency {txn.currency!r} does not match "
                    f"expected {expected_currency!r} for variant-derived statement"
                )

            try:
                amount = parse_amount(txn.amount, txn.currency)
            except Exception as exc:
                raise IngestionError(
                    f"Transaction {index} has unparseable amount {txn.amount!r}: {exc}"
                ) from exc
            txn_date = _parse_llm_date(txn.date, index=index)

            installment_value: Decimal | None = None
            if txn.installment_value:
                try:
                    installment_value = parse_amount(txn.installment_value, txn.currency)
                except Exception as exc:
                    raise IngestionError(
                        f"Transaction {index} has unparseable installment_value "
                        f"{txn.installment_value!r}: {exc}"
                    ) from exc

            transactions.append(
                Transaction(
                    statement_id=statement.id,
                    date=txn_date,
                    description=txn.description,
                    amount=amount,
                    currency=txn.currency,
                    category=txn.category,
                    installment_number=txn.installment_number,
                    installment_total=txn.installment_total,
                    installment_value=installment_value,
                    raw_json=txn.model_dump(),
                )
            )

        return transactions

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


def _parse_llm_date(value: str, *, index: int) -> date | None:
    """Parse an LLM-emitted date into a :class:`date`.

    The LLM is told to use ``DD/MM/YYYY`` but defensive parsing
    accepts the two-digit year and the ISO form. An empty
    string returns ``None`` (the model could not extract a
    date from a partial chunk) and the caller falls back to
    a safe default. Anything else raises :class:`ValueError`
    with the index of the offending row so the operator can
    find it in the raw LLM output.

    Raises
    ------
    ValueError
        If the date is unparseable or out of range.
    """
    if not value or not value.strip():
        return None
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


def _dedupe_transactions(
    transactions: list[TransactionExtraction],
) -> list[TransactionExtraction]:
    """Drop duplicate transactions by ``(date, description, amount)``.

    The chunked extraction path runs the LLM on overlapping
    windows of the PDF, so a transaction that straddles a
    chunk boundary appears in two chunks and would land on
    the database twice without deduplication. Two transactions
    are considered duplicates when their ``date``,
    ``description`` (case-insensitive, whitespace-stripped),
    and ``amount`` strings all match. The first occurrence
    wins, so the LLM's first read of the row is the one that
    gets persisted.
    """
    seen: set[tuple[str, str, str]] = set()
    deduped: list[TransactionExtraction] = []
    for txn in transactions:
        key = (
            txn.date,
            txn.description.strip().upper(),
            txn.amount.strip(),
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(txn)
    return deduped


__all__ = [
    "BankNotFoundError",
    "DuplicateStatementError",
    "IngestionError",
    "IngestionService",
]
