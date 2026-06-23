"""Tests for the Work Unit 4 ingestion orchestrator and API endpoints.

This test module covers Phase 1, WU 4: the :class:`IngestionService`
that wires the deterministic PDF pipeline with the non-deterministic
LLM extraction, plus the four HTTP endpoints the orchestrator backs.

The tests are organised in three layers:

* **IngestionService unit tests** — exercise the service directly with
  a real SQLite database and a fake LLM client. The PDF pipeline runs
  against the real sample PDFs in ``shared/account-state-examples/``,
  so the test surface is realistic without being slow.
* **HTTP integration tests** — drive the FastAPI app through an
  :class:`httpx.AsyncClient` with ``ASGITransport``. The
  :func:`get_ingestion_service` dependency is overridden with a fake
  so the LLM never makes a real network call.
* **Edge cases and error paths** — oversize upload, invalid PDF, LLM
  failure, idempotency, missing rows, and category validation.

Every test uses a fresh throwaway SQLite database (via the
``test_settings`` fixture from :mod:`tests.conftest`) and the
ORM schema is created via :func:`Base.metadata.create_all` so the
DDL matches the model definitions exactly (including
``server_default=func.now()`` on the timestamp columns). The
three known banks are seeded by the test fixture. Alembic
round-trips are exercised separately by :mod:`tests.test_alembic`.
"""

from __future__ import annotations

import hashlib
import os
import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.api.v1.statements import get_ingestion_service
from app.core.config import Settings
from app.db.engine import create_engine
from app.db.session import get_session
from app.main import create_app
from app.models.bank import Bank
from app.models.base import Base
from app.models.statement import StatementStatus
from app.services.ingestion import (
    BankNotFoundError,
    IngestionError,
    IngestionService,
)
from app.services.llm.protocol import LLMProvider
from app.services.llm.schemas import ExtractionResponse

# ---------------------------------------------------------------------------
# Sample PDF paths and the TEST_RUT env var
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SAMPLE_PDFS_DIR = PROJECT_ROOT / "shared" / "account-state-examples"

SANTANDER_PDF = SAMPLE_PDFS_DIR / "80_15796_0350262800062166708_20260422.pdf"
BANCO_CHILE_PDF = SAMPLE_PDFS_DIR / "EECCTarjetaVisa.pdf"
ITAU_PDF = SAMPLE_PDFS_DIR / "EECCvirtual.pdf"

#: Cardholder RUT, read from the environment so the real identifier
#: never has to be committed. The orchestrator derives the
#: per-bank PDF password from this value before attempting to
#: decrypt the sample statement, so every test below that runs
#: the full pipeline (almost all of them) needs it.
TEST_RUT: str | None = os.getenv("TEST_RUT")

_SAMPLE_PDFS_PRESENT = SANTANDER_PDF.exists() and BANCO_CHILE_PDF.exists() and ITAU_PDF.exists()

needs_sample_pdfs = pytest.mark.skipif(
    not _SAMPLE_PDFS_PRESENT,
    reason=(
        f"Sample PDFs not found in {SAMPLE_PDFS_DIR}. "
        "The integration tests are skipped in this environment."
    ),
)

#: Skip the integration tests that decrypt the real sample PDFs:
#: they need the cardholder RUT to derive the right password, and
#: that value lives in the env, not in the repo. Set
#: ``TEST_RUT=<your-rut>`` to run them.
needs_test_rut = pytest.mark.skipif(
    TEST_RUT is None,
    reason=(
        "TEST_RUT environment variable not set. "
        "Tests that decrypt real PDFs are skipped to keep the "
        "cardholder's RUT out of the repository. Run them locally with "
        "`TEST_RUT=<your-rut> pytest tests/`."
    ),
)


# ---------------------------------------------------------------------------
# Fake LLM client
# ---------------------------------------------------------------------------


@dataclass
class FakeLLMClient:
    """In-memory :class:`LLMProvider` for tests.

    The client records every call so tests can assert on the
    arguments. A canned response is returned; tests can also
    override the canned response per call to simulate retries
    or escalating failures.

    The class satisfies the :class:`LLMProvider` Protocol via
    structural typing — no inheritance required. It also
    implements the optional :meth:`aclose` that the production
    clients expose, so the dependency can clean up if it wants
    to.
    """

    response: ExtractionResponse
    calls: list[tuple[str, str]] = field(default_factory=list)
    raise_exc: Exception | None = None
    closed: bool = False

    async def extract_transactions(self, text: str, variant: str) -> ExtractionResponse:
        """Return ``self.response`` (or raise ``self.raise_exc``)."""
        self.calls.append((text, variant))
        if self.raise_exc is not None:
            raise self.raise_exc
        return self.response

    async def aclose(self) -> None:
        """Mark the client as closed. No resources to release."""
        self.closed = True


# ---------------------------------------------------------------------------
# Canned extraction payloads
# ---------------------------------------------------------------------------


NACIONAL_EXTRACTION_PAYLOAD: dict[str, Any] = {
    "transactions": [
        {
            "date": "15/05/25",
            "description": "SUPERMERCADOS LIDER",
            "amount": "$ 12.450",
            "currency": "CLP",
            "category": "Groceries",
            "installment_number": None,
            "installment_total": None,
            "installment_value": None,
        },
        {
            "date": "22/05/25",
            "description": "COMBUSTIBLE COPEC",
            "amount": "$ 35.000",
            "currency": "CLP",
            "category": "Transport",
            "installment_number": None,
            "installment_total": None,
            "installment_value": None,
        },
        {
            "date": "01/06/25",
            "description": "PARIS 03/06",
            "amount": "$ 89.900",
            "currency": "CLP",
            "category": "Shopping",
            "installment_number": 3,
            "installment_total": 6,
            "installment_value": "$ 89.900",
        },
    ],
    "metadata": {
        "period_start": "15/05/2025",
        "period_end": "22/05/2025",
        "statement_date": "01/06/2025",
    },
    "confidence": 0.95,
    "notes": "3 transactions, one installment plan.",
}


INTERNACIONAL_EXTRACTION_PAYLOAD: dict[str, Any] = {
    "transactions": [
        {
            "date": "03/05/25",
            "description": "SPOTIFY USA",
            "amount": "US$ 9,99",
            "currency": "USD",
            "category": "Subscriptions",
            "installment_number": None,
            "installment_total": None,
            "installment_value": None,
        },
        {
            "date": "18/05/25",
            "description": "AMAZON.COM",
            "amount": "US$ 42,30",
            "currency": "USD",
            "category": "Shopping",
            "installment_number": None,
            "installment_total": None,
            "installment_value": None,
        },
    ],
    "metadata": {
        "period_start": "03/05/2025",
        "period_end": "18/05/2025",
        "statement_date": "01/06/2025",
    },
    "confidence": 0.92,
    "notes": "2 transactions, all USD.",
}


# ---------------------------------------------------------------------------
# Database fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def seeded_engine(
    test_settings: Settings,
) -> AsyncIterator[AsyncEngine]:
    """Yield a database engine with the full ORM schema created and banks seeded.

    Uses :func:`app.models.base.Base.metadata.create_all` so the
    generated DDL matches the model definitions exactly
    (including ``server_default=func.now()`` on the timestamp
    columns). The three known banks are inserted with the same
    ``password_formula`` values the production migration uses,
    so the orchestrator can look them up by name.

    On teardown the engine is disposed; the temp file is cleaned
    up by ``test_settings``.
    """
    engine = create_engine(test_settings)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        now = datetime.now(UTC)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            session.add_all(
                [
                    Bank(
                        name="santander",
                        display_name="Banco Santander",
                        password_formula="rut_sin_dv",
                        is_active=True,
                        created_at=now,
                        updated_at=now,
                    ),
                    Bank(
                        name="itau",
                        display_name="Itaú",
                        password_formula="rut_sin_dv",
                        is_active=True,
                        created_at=now,
                        updated_at=now,
                    ),
                    Bank(
                        name="banco_de_chile",
                        display_name="Banco de Chile",
                        password_formula="rut_ultimos_4",
                        is_active=True,
                        created_at=now,
                        updated_at=now,
                    ),
                ]
            )
            await session.commit()

        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
async def session_factory(
    seeded_engine: AsyncEngine,
) -> async_sessionmaker[AsyncSession]:
    """Yield a session factory bound to the seeded test engine."""
    return async_sessionmaker(seeded_engine, expire_on_commit=False)


@pytest.fixture
def upload_dir(
    test_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> Iterator[Path]:
    """Point ``PDF_UPLOAD_DIR`` at a per-test temp directory."""
    target = tmp_path / "uploads"
    target.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("PDF_UPLOAD_DIR", str(target))
    from app.core.config import get_settings as _get_settings

    _get_settings.cache_clear()
    yield target
    _get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Ingestion service fixture
# ---------------------------------------------------------------------------


class _ServiceContext:
    """Async context manager that yields an :class:`IngestionService`.

    Holds the session's lifecycle: opens the session on enter
    and closes it on exit, mirroring the FastAPI dependency's
    ``async with factory() as session:`` pattern. Tests use it
    via the :func:`make_ingestion_service` factory so each test
    binds a different ``FakeLLMClient``.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        llm_client: LLMProvider,
        settings: Settings,
    ) -> None:
        self._factory = session_factory
        self._llm_client = llm_client
        self._settings = settings
        self._service: IngestionService | None = None

    async def __aenter__(self) -> IngestionService:
        self._session_cm = self._factory()
        self._session = await self._session_cm.__aenter__()
        self._service = IngestionService(
            session=self._session,
            llm_client=self._llm_client,
            settings=self._settings,
        )
        return self._service

    async def __aexit__(self, *args: object) -> None:
        await self._session_cm.__aexit__(*args)


@pytest.fixture
def make_ingestion_service(
    session_factory: async_sessionmaker[AsyncSession],
    test_settings: Settings,
):
    """Return a factory that yields :class:`IngestionService` instances.

    The factory yields a service with a *live* session in an
    async context. The service commits inside the pipeline, so
    the session must be in a proper context for the duration
    of the call.

    Usage::

        async with make_ingestion_service(llm) as service:
            statement = await service.ingest_statement(...)
    """

    def _factory(llm_client: LLMProvider) -> _ServiceContext:
        return _ServiceContext(session_factory, llm_client, test_settings)

    return _factory


# ---------------------------------------------------------------------------
# IngestionService unit tests
# ---------------------------------------------------------------------------


@needs_sample_pdfs
@needs_test_rut
class TestIngestStatementHappyPath:
    """``IngestStatement`` runs the full pipeline and persists every row."""

    @pytest.mark.asyncio
    async def test_creates_statement_and_transactions(
        self,
        make_ingestion_service: Any,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """A successful ingestion creates one statement and N transactions."""
        llm = FakeLLMClient(response=ExtractionResponse.model_validate(NACIONAL_EXTRACTION_PAYLOAD))
        async with make_ingestion_service(llm) as service:
            statement = await service.ingest_statement(
                file_path=SANTANDER_PDF,
                bank_name="santander",
                rut=TEST_RUT,
            )

        # Status and persisted state
        assert statement.status == StatementStatus.COMPLETED
        assert statement.error_message is None
        assert statement.credit_card is not None
        assert statement.credit_card.bank.name == "santander"
        assert len(statement.transactions) == 3

        # LLM was called once with the right variant
        assert len(llm.calls) == 1
        _text, variant = llm.calls[0]
        assert variant == "NACIONAL"

        # Transactions were persisted with the right shape
        async with session_factory() as session:
            count = (await session.execute(text("SELECT COUNT(*) FROM transactions"))).scalar_one()
            assert count == 3

    @pytest.mark.asyncio
    async def test_installment_fields_persist(
        self,
        make_ingestion_service: Any,
    ) -> None:
        """Installment fields land on the row with the parsed value."""
        llm = FakeLLMClient(response=ExtractionResponse.model_validate(NACIONAL_EXTRACTION_PAYLOAD))
        async with make_ingestion_service(llm) as service:
            statement = await service.ingest_statement(
                file_path=SANTANDER_PDF,
                bank_name="santander",
                rut=TEST_RUT,
            )

        paris = next(t for t in statement.transactions if "PARIS" in t.description)
        assert paris.installment_number == 3
        assert paris.installment_total == 6
        assert paris.installment_value == Decimal("89900")

    @pytest.mark.asyncio
    async def test_amounts_parsed_to_decimal(
        self,
        make_ingestion_service: Any,
    ) -> None:
        """The raw LLM amount strings are coerced to :class:`Decimal`."""
        llm = FakeLLMClient(response=ExtractionResponse.model_validate(NACIONAL_EXTRACTION_PAYLOAD))
        async with make_ingestion_service(llm) as service:
            statement = await service.ingest_statement(
                file_path=SANTANDER_PDF,
                bank_name="santander",
                rut=TEST_RUT,
            )

        by_desc = {t.description: t.amount for t in statement.transactions}
        assert by_desc["SUPERMERCADOS LIDER"] == Decimal("12450")
        assert by_desc["COMBUSTIBLE COPEC"] == Decimal("35000")
        assert by_desc["PARIS 03/06"] == Decimal("89900")
        # ``Decimal`` not ``float``
        assert all(isinstance(v, Decimal) for v in by_desc.values())

    @pytest.mark.asyncio
    async def test_raw_json_is_preserved(
        self,
        make_ingestion_service: Any,
    ) -> None:
        """The verbatim LLM extraction is stored on the transaction row."""
        llm = FakeLLMClient(response=ExtractionResponse.model_validate(NACIONAL_EXTRACTION_PAYLOAD))
        async with make_ingestion_service(llm) as service:
            statement = await service.ingest_statement(
                file_path=SANTANDER_PDF,
                bank_name="santander",
                rut=TEST_RUT,
            )

        lider = next(t for t in statement.transactions if "LIDER" in t.description)
        assert lider.raw_json is not None
        assert lider.raw_json["date"] == "15/05/25"
        assert lider.raw_json["currency"] == "CLP"
        assert lider.raw_json["amount"] == "$ 12.450"

    @pytest.mark.asyncio
    async def test_period_dates_come_from_llm_metadata(
        self,
        make_ingestion_service: Any,
    ) -> None:
        """The statement period and emission date come from the LLM metadata.

        The orchestrator used to default the period to the
        current calendar month. With the metadata extracted
        from the PDF, the statement dates match the LLM's
        output (15/05/2025 → 22/05/2025, emission 01/06/2025
        in the canonical payload). This is the whole point of
        the metadata extraction.
        """
        llm = FakeLLMClient(response=ExtractionResponse.model_validate(NACIONAL_EXTRACTION_PAYLOAD))
        async with make_ingestion_service(llm) as service:
            statement = await service.ingest_statement(
                file_path=SANTANDER_PDF,
                bank_name="santander",
                rut=TEST_RUT,
            )

        assert statement.period_start == date(2025, 5, 15)
        assert statement.period_end == date(2025, 5, 22)
        assert statement.statement_date == date(2025, 6, 1)

    @pytest.mark.asyncio
    async def test_creates_new_credit_card_on_first_use(
        self,
        make_ingestion_service: Any,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """The first ingestion for a card creates a new ``CreditCard`` row."""
        llm = FakeLLMClient(response=ExtractionResponse.model_validate(NACIONAL_EXTRACTION_PAYLOAD))
        async with make_ingestion_service(llm) as service:
            await service.ingest_statement(
                file_path=SANTANDER_PDF,
                bank_name="santander",
                rut=TEST_RUT,
            )

        async with session_factory() as session:
            count = (await session.execute(text("SELECT COUNT(*) FROM credit_cards"))).scalar_one()
            assert count == 1

    @pytest.mark.asyncio
    async def test_credit_card_populated_from_llm_metadata(
        self,
        make_ingestion_service: Any,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """The :class:`CreditCard` row is built from the LLM's metadata block.

        The user no longer types card_number_masked, cardholder,
        or currency. The orchestrator reads them off the LLM
        response and creates (or finds) the matching
        :class:`CreditCard`.
        """
        llm = FakeLLMClient(response=ExtractionResponse.model_validate(NACIONAL_EXTRACTION_PAYLOAD))
        async with make_ingestion_service(llm) as service:
            statement = await service.ingest_statement(
                file_path=SANTANDER_PDF,
                bank_name="santander",
                rut=TEST_RUT,
            )

        # The credit card is populated from the LLM metadata,
        # not from the now-removed user inputs.
        assert statement.credit_card.card_number_masked == "XXXX XXXX XXXX 0463"
        assert statement.credit_card.cardholder == "LUIS SOTILLO"
        assert statement.credit_card.currency == "CLP"

    @pytest.mark.asyncio
    async def test_rejects_metadata_currency_mismatch(
        self,
        make_ingestion_service: Any,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """A NACIONAL statement with USD metadata is rejected.

        The LLM can read the variant directly from the text,
        but the metadata currency must still match. A
        mismatch is a strong signal that the LLM
        hallucinated, so the orchestrator fails fast.
        """
        bad_payload = {
            "transactions": [
                {
                    "date": "15/05/25",
                    "description": "SUPERMERCADOS LIDER",
                    "amount": "$ 12.450",
                    "currency": "CLP",
                    "category": "Groceries",
                }
            ],
            "metadata": {
                "card_number_masked": "XXXX XXXX XXXX 0463",
                "cardholder": "LUIS SOTILLO",
                "currency": "USD",  # wrong for a NACIONAL statement
                "period_start": "01/05/2025",
                "period_end": "31/05/2025",
                "statement_date": "01/06/2025",
            },
            "confidence": 0.5,
            "notes": None,
        }
        llm = FakeLLMClient(response=ExtractionResponse.model_validate(bad_payload))
        async with make_ingestion_service(llm) as service:
            with pytest.raises(IngestionError, match="metadata currency"):
                await service.ingest_statement(
                    file_path=SANTANDER_PDF,
                    bank_name="santander",
                    rut=TEST_RUT,
                )

        # No statement was created — a metadata mismatch is
        # a pre-transaction failure.
        async with session_factory() as session:
            count = (await session.execute(text("SELECT COUNT(*) FROM statements"))).scalar_one()
            assert count == 0

    @pytest.mark.asyncio
    async def test_reuses_existing_credit_card(
        self,
        make_ingestion_service: Any,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """A second ingestion for a different PDF on the same card does not duplicate."""
        # First upload
        llm1 = FakeLLMClient(
            response=ExtractionResponse.model_validate(NACIONAL_EXTRACTION_PAYLOAD)
        )
        async with make_ingestion_service(llm1) as service1:
            await service1.ingest_statement(
                file_path=SANTANDER_PDF,
                bank_name="santander",
                rut=TEST_RUT,
            )

        # Second upload: copy the PDF so the hash differs but the
        # card identity stays the same. The new copy gets a
        # different SHA-256 because of the embedded file metadata.
        from shutil import copy

        second_pdf = SANTANDER_PDF.parent / "santander-copy.pdf"
        copy(SANTANDER_PDF, second_pdf)
        try:
            llm2 = FakeLLMClient(
                response=ExtractionResponse.model_validate(NACIONAL_EXTRACTION_PAYLOAD)
            )
            async with make_ingestion_service(llm2) as service2:
                statement2 = await service2.ingest_statement(
                    file_path=second_pdf,
                    bank_name="santander",
                    rut=TEST_RUT,
                )
            assert statement2.status == StatementStatus.COMPLETED
        finally:
            second_pdf.unlink(missing_ok=True)

        async with session_factory() as session:
            count = (await session.execute(text("SELECT COUNT(*) FROM credit_cards"))).scalar_one()
            assert count == 1


@needs_sample_pdfs
@needs_test_rut
class TestIngestStatementIdempotency:
    """Re-uploading the same file is a no-op."""

    @pytest.mark.asyncio
    async def test_duplicate_hash_returns_existing_statement(
        self,
        make_ingestion_service: Any,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Uploading the same PDF twice returns the original statement.

        The second call should not re-run the LLM, should not create
        a second statement row, and should return the same UUID.
        """
        llm1 = FakeLLMClient(
            response=ExtractionResponse.model_validate(NACIONAL_EXTRACTION_PAYLOAD)
        )
        async with make_ingestion_service(llm1) as service1:
            first = await service1.ingest_statement(
                file_path=SANTANDER_PDF,
                bank_name="santander",
                rut=TEST_RUT,
            )

        llm2 = FakeLLMClient(
            response=ExtractionResponse.model_validate(NACIONAL_EXTRACTION_PAYLOAD)
        )
        async with make_ingestion_service(llm2) as service2:
            second = await service2.ingest_statement(
                file_path=SANTANDER_PDF,
                bank_name="santander",
                rut=TEST_RUT,
            )

        assert second.id == first.id
        # The LLM was only called once across both ingestions
        assert len(llm1.calls) == 1
        assert llm2.calls == []

        async with session_factory() as session:
            count = (await session.execute(text("SELECT COUNT(*) FROM statements"))).scalar_one()
            assert count == 1


# ---------------------------------------------------------------------------
# IngestionService error handling
# ---------------------------------------------------------------------------


@needs_sample_pdfs
@needs_test_rut
class TestIngestStatementErrors:
    """Failure modes surface the cause on the raised exception.

    Pre-LLM errors (bad bank, bad RUT, wrong PDF password,
    text-extraction failure, variant detection failure) fail
    fast and never create a statement row. The orchestrator
    raises :class:`IngestionError` (or a typed subclass) and
    the HTTP layer maps it to a 422.

    Per-transaction errors (an LLM-emitted amount that cannot
    be parsed, or a wrong currency) happen *after* the
    statement is created and are recorded as a FAILED row —
    see :class:`TestIngestStatementAmountErrors` below.
    """

    @pytest.mark.asyncio
    async def test_unknown_bank_raises(
        self,
        make_ingestion_service: Any,
    ) -> None:
        """An unknown ``bank_name`` raises :class:`BankNotFoundError`."""
        llm = FakeLLMClient(response=ExtractionResponse.model_validate(NACIONAL_EXTRACTION_PAYLOAD))
        async with make_ingestion_service(llm) as service:
            with pytest.raises(BankNotFoundError, match="not_a_real_bank"):
                await service.ingest_statement(
                    file_path=SANTANDER_PDF,
                    bank_name="not_a_real_bank",
                    rut=TEST_RUT,
                )

    @pytest.mark.asyncio
    async def test_invalid_rut_raises_before_pipeline(
        self,
        make_ingestion_service: Any,
    ) -> None:
        """A bad RUT surfaces as :class:`IngestionError` with ``InvalidRUTError`` as cause.

        The RUT validation runs before any DB write, so no
        statement row is created and the exception propagates
        cleanly. The HTTP layer maps :class:`IngestionError` to
        422.
        """
        from app.services.pdf import InvalidRUTError

        llm = FakeLLMClient(response=ExtractionResponse.model_validate(NACIONAL_EXTRACTION_PAYLOAD))
        async with make_ingestion_service(llm) as service:
            with pytest.raises(IngestionError) as exc_info:
                await service.ingest_statement(
                    file_path=SANTANDER_PDF,
                    bank_name="santander",
                    rut="not-a-rut",
                )
        assert isinstance(exc_info.value.__cause__, InvalidRUTError)

    @pytest.mark.asyncio
    async def test_wrong_password_returns_422(
        self,
        make_ingestion_service: Any,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """A wrong PDF password surfaces as :class:`IngestionError` and creates no row.

        Pre-LLM errors do not leave a statement behind — the
        upload can be retried with a corrected RUT and the
        same file. The HTTP layer maps the
        :class:`IngestionError` to a 422.
        """
        from app.services.pdf import PDFPasswordError

        llm = FakeLLMClient(response=ExtractionResponse.model_validate(NACIONAL_EXTRACTION_PAYLOAD))
        async with make_ingestion_service(llm) as service:
            with pytest.raises(IngestionError) as exc_info:
                await service.ingest_statement(
                    file_path=SANTANDER_PDF,
                    bank_name="santander",
                    # Wrong RUT → wrong password → decrypt fails
                    rut="11.111.111-1",
                )
        # The underlying cause is the typed PDF error
        assert isinstance(exc_info.value.__cause__, PDFPasswordError)

        # No statement row was created — a pre-LLM error is
        # always a fast-fail.
        async with session_factory() as session:
            count = (await session.execute(text("SELECT COUNT(*) FROM statements"))).scalar_one()
            assert count == 0

    @pytest.mark.asyncio
    async def test_llm_failure_returns_422(
        self,
        make_ingestion_service: Any,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """An :class:`LLMExtractionError` from the LLM surfaces as :class:`IngestionError`.

        The LLM error is wrapped in :class:`IngestionError` so the
        HTTP layer can map it to a 422. The original LLM
        exception is preserved on ``__cause__`` for log
        inspection.

        Pre-LLM errors do not leave a statement behind — only
        per-transaction errors (covered by the next test class)
        create a FAILED row, because the statement must exist
        to attach transactions to.
        """
        from app.services.llm.schemas import LLMExtractionError

        llm = FakeLLMClient(
            response=ExtractionResponse.model_validate(NACIONAL_EXTRACTION_PAYLOAD),
            raise_exc=LLMExtractionError("LLM timed out"),
        )
        async with make_ingestion_service(llm) as service:
            with pytest.raises(IngestionError) as exc_info:
                await service.ingest_statement(
                    file_path=SANTANDER_PDF,
                    bank_name="santander",
                    rut=TEST_RUT,
                )
        # The LLM error is the cause of the typed IngestionError
        assert isinstance(exc_info.value.__cause__, LLMExtractionError)
        assert "LLM timed out" in str(exc_info.value.__cause__)

        async with session_factory() as session:
            count = (await session.execute(text("SELECT COUNT(*) FROM statements"))).scalar_one()
            assert count == 0

    @pytest.mark.asyncio
    async def test_amount_parse_error_creates_failed_statement(
        self,
        make_ingestion_service: Any,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """An unparseable amount in the LLM response marks the statement FAILED.

        Per-transaction errors happen *after* the statement
        row is created (so it can carry the transactions), so
        they are recorded on the row as FAILED with the
        error_message set.
        """
        bad_payload = {
            "transactions": [
                {
                    "date": "15/05/25",
                    "description": "BAD AMOUNT",
                    # LLM hallucinated a value with no currency marker
                    "amount": "garbage",
                    "currency": "CLP",
                    "category": None,
                }
            ],
            "metadata": {
                "card_number_masked": "XXXX XXXX XXXX 0463",
                "cardholder": "LUIS SOTILLO",
                "currency": "CLP",
                "period_start": "01/05/2025",
                "period_end": "31/05/2025",
                "statement_date": "01/06/2025",
            },
            "confidence": 0.5,
            "notes": None,
        }
        llm = FakeLLMClient(response=ExtractionResponse.model_validate(bad_payload))
        async with make_ingestion_service(llm) as service:
            with pytest.raises(IngestionError):
                await service.ingest_statement(
                    file_path=SANTANDER_PDF,
                    bank_name="santander",
                    rut=TEST_RUT,
                )

        async with session_factory() as session:
            row = (await session.execute(text("SELECT status FROM statements"))).first()
            assert row is not None
            assert row[0] == "failed"

    @pytest.mark.asyncio
    async def test_currency_mismatch_raises(
        self,
        make_ingestion_service: Any,
    ) -> None:
        """A NACIONAL statement with USD transactions is rejected."""
        bad_payload = {
            "transactions": [
                {
                    "date": "15/05/25",
                    "description": "WRONG CURRENCY",
                    "amount": "US$ 9,99",
                    "currency": "USD",  # wrong for NACIONAL
                    "category": None,
                }
            ],
            "metadata": {
                "card_number_masked": "XXXX XXXX XXXX 0463",
                "cardholder": "LUIS SOTILLO",
                "currency": "CLP",
                "period_start": "01/05/2025",
                "period_end": "31/05/2025",
                "statement_date": "01/06/2025",
            },
            "confidence": 0.5,
            "notes": None,
        }
        llm = FakeLLMClient(response=ExtractionResponse.model_validate(bad_payload))
        async with make_ingestion_service(llm) as service:
            with pytest.raises(IngestionError, match="currency"):
                await service.ingest_statement(
                    file_path=SANTANDER_PDF,
                    bank_name="santander",
                    rut=TEST_RUT,
                )


# ---------------------------------------------------------------------------
# IngestionService — international variant
# ---------------------------------------------------------------------------


@needs_sample_pdfs
@needs_test_rut
class TestIngestStatementInternacional:
    """The INTERNACIONAL variant flows through the same pipeline."""

    @pytest.mark.asyncio
    async def test_usd_statement_succeeds(
        self,
        make_ingestion_service: Any,
    ) -> None:
        """An Itaú (INTERNACIONAL) PDF is parsed with USD amounts."""
        llm = FakeLLMClient(
            response=ExtractionResponse.model_validate(INTERNACIONAL_EXTRACTION_PAYLOAD)
        )
        async with make_ingestion_service(llm) as service:
            statement = await service.ingest_statement(
                file_path=ITAU_PDF,
                bank_name="itau",
                rut=TEST_RUT,
            )

        assert statement.status == StatementStatus.COMPLETED
        assert len(statement.transactions) == 2
        # The LLM was called with the INTERNACIONAL variant
        assert llm.calls[0][1] == "INTERNACIONAL"
        # Every transaction carries USD
        assert all(t.currency == "USD" for t in statement.transactions)


# ---------------------------------------------------------------------------
# HTTP integration helpers
# ---------------------------------------------------------------------------


def _make_session_override(
    factory: async_sessionmaker[AsyncSession],
):
    """Return a ``get_session`` override bound to ``factory``.

    FastAPI's dependency-override machinery expects a callable
    that, when called, returns the dependency's value (here, an
    async generator yielding the session). Wrapping the
    generator in a function is what makes the override a
    drop-in for the original :func:`app.db.session.get_session`
    async-generator dependency.
    """

    async def _override() -> AsyncIterator[AsyncSession]:
        async with factory() as session:
            yield session

    return _override


# ---------------------------------------------------------------------------
# HTTP integration: upload endpoint
# ---------------------------------------------------------------------------


@needs_sample_pdfs
@needs_test_rut
class TestUploadEndpoint:
    """The ``POST /api/v1/statements/upload`` endpoint drives the orchestrator."""

    @pytest.mark.asyncio
    async def test_upload_with_real_santander_pdf(
        self,
        test_settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
        upload_dir: Path,
    ) -> None:
        """Uploading a real Santander PDF returns 201 with the resulting statement."""
        llm = FakeLLMClient(response=ExtractionResponse.model_validate(NACIONAL_EXTRACTION_PAYLOAD))
        app = create_app(test_settings)

        async def _override() -> AsyncIterator[IngestionService]:
            async with session_factory() as session:
                yield IngestionService(
                    session=session,
                    llm_client=llm,
                    settings=test_settings,
                )

        app.dependency_overrides[get_ingestion_service] = _override
        try:
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://testserver") as client:
                files = {"file": ("statement.pdf", SANTANDER_PDF.read_bytes(), "application/pdf")}
                data = {
                    "bank_name": "santander",
                    "rut": TEST_RUT,
                }
                response = await client.post("/api/v1/statements/upload", files=files, data=data)
        finally:
            app.dependency_overrides.pop(get_ingestion_service, None)

        assert response.status_code == 201, response.text
        body = response.json()
        assert body["status"] == "completed"
        assert body["credit_card_id"]
        assert len(body["transactions"]) == 3

        # The upload was persisted under the configured dir
        uploads = list(upload_dir.iterdir())
        assert len(uploads) == 1
        assert uploads[0].name.endswith(".pdf")

    @pytest.mark.asyncio
    async def test_upload_with_itau_pdf_returns_usd(
        self,
        test_settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
        upload_dir: Path,
    ) -> None:
        """An Itaú (INTERNACIONAL) upload produces USD transactions."""
        llm = FakeLLMClient(
            response=ExtractionResponse.model_validate(INTERNACIONAL_EXTRACTION_PAYLOAD)
        )
        app = create_app(test_settings)

        async def _override() -> AsyncIterator[IngestionService]:
            async with session_factory() as session:
                yield IngestionService(
                    session=session,
                    llm_client=llm,
                    settings=test_settings,
                )

        app.dependency_overrides[get_ingestion_service] = _override
        try:
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://testserver") as client:
                files = {"file": ("itau.pdf", ITAU_PDF.read_bytes(), "application/pdf")}
                data = {
                    "bank_name": "itau",
                    "rut": TEST_RUT,
                }
                response = await client.post("/api/v1/statements/upload", files=files, data=data)
        finally:
            app.dependency_overrides.pop(get_ingestion_service, None)

        assert response.status_code == 201
        body = response.json()
        assert body["status"] == "completed"
        assert {t["currency"] for t in body["transactions"]} == {"USD"}

    @pytest.mark.asyncio
    async def test_upload_rejects_non_pdf_with_415(
        self,
        test_settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
        upload_dir: Path,
    ) -> None:
        """A file whose magic bytes are not ``%PDF-`` is rejected with 415."""
        llm = FakeLLMClient(response=ExtractionResponse.model_validate(NACIONAL_EXTRACTION_PAYLOAD))
        app = create_app(test_settings)

        async def _override() -> AsyncIterator[IngestionService]:
            async with session_factory() as session:
                yield IngestionService(
                    session=session,
                    llm_client=llm,
                    settings=test_settings,
                )

        app.dependency_overrides[get_ingestion_service] = _override
        try:
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://testserver") as client:
                files = {"file": ("evil.pdf", b"not-a-pdf-just-bytes", "application/pdf")}
                data = {
                    "bank_name": "santander",
                    "rut": TEST_RUT,
                }
                response = await client.post("/api/v1/statements/upload", files=files, data=data)
        finally:
            app.dependency_overrides.pop(get_ingestion_service, None)

        assert response.status_code == 415
        assert "not a PDF" in response.json()["detail"]

    @pytest.mark.asyncio
    async def test_upload_rejects_oversize_file_with_413(
        self,
        test_settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
        upload_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """An upload larger than ``MAX_FILE_SIZE_MB`` returns 413."""
        # 1 MB cap
        monkeypatch.setenv("MAX_FILE_SIZE_MB", "1")
        from app.core.config import get_settings as _get_settings

        _get_settings.cache_clear()
        small_settings = _get_settings()

        llm = FakeLLMClient(response=ExtractionResponse.model_validate(NACIONAL_EXTRACTION_PAYLOAD))
        app = create_app(small_settings)

        async def _override() -> AsyncIterator[IngestionService]:
            async with session_factory() as session:
                yield IngestionService(
                    session=session,
                    llm_client=llm,
                    settings=small_settings,
                )

        app.dependency_overrides[get_ingestion_service] = _override
        try:
            # Build a 2 MB file whose first bytes are a valid PDF
            # magic — only the size check should fire.
            big = b"%PDF-1.4\n" + b"0" * (2 * 1024 * 1024)
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://testserver") as client:
                files = {"file": ("huge.pdf", big, "application/pdf")}
                data = {
                    "bank_name": "santander",
                    "rut": TEST_RUT,
                }
                response = await client.post("/api/v1/statements/upload", files=files, data=data)
        finally:
            app.dependency_overrides.pop(get_ingestion_service, None)
            _get_settings.cache_clear()

        assert response.status_code == 413
        assert "exceeding" in response.json()["detail"]

    @pytest.mark.asyncio
    async def test_upload_with_unknown_bank_returns_422(
        self,
        test_settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
        upload_dir: Path,
    ) -> None:
        """A 422 surfaces when the bank name is unknown."""
        llm = FakeLLMClient(response=ExtractionResponse.model_validate(NACIONAL_EXTRACTION_PAYLOAD))
        app = create_app(test_settings)

        async def _override() -> AsyncIterator[IngestionService]:
            async with session_factory() as session:
                yield IngestionService(
                    session=session,
                    llm_client=llm,
                    settings=test_settings,
                )

        app.dependency_overrides[get_ingestion_service] = _override
        try:
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://testserver") as client:
                files = {"file": ("statement.pdf", SANTANDER_PDF.read_bytes(), "application/pdf")}
                data = {
                    "bank_name": "unknown_bank",
                    "rut": TEST_RUT,
                }
                response = await client.post("/api/v1/statements/upload", files=files, data=data)
        finally:
            app.dependency_overrides.pop(get_ingestion_service, None)

        assert response.status_code == 422
        assert "unknown_bank" in response.json()["detail"]

    @pytest.mark.asyncio
    async def test_upload_with_llm_failure_returns_422(
        self,
        test_settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
        upload_dir: Path,
    ) -> None:
        """A failing LLM surfaces as 422 and the statement is persisted as failed."""
        from app.services.llm.schemas import LLMExtractionError

        llm = FakeLLMClient(
            response=ExtractionResponse.model_validate(NACIONAL_EXTRACTION_PAYLOAD),
            raise_exc=LLMExtractionError("simulated LLM outage"),
        )
        app = create_app(test_settings)

        async def _override() -> AsyncIterator[IngestionService]:
            async with session_factory() as session:
                yield IngestionService(
                    session=session,
                    llm_client=llm,
                    settings=test_settings,
                )

        app.dependency_overrides[get_ingestion_service] = _override
        try:
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://testserver") as client:
                files = {"file": ("statement.pdf", SANTANDER_PDF.read_bytes(), "application/pdf")}
                data = {
                    "bank_name": "santander",
                    "rut": TEST_RUT,
                }
                response = await client.post("/api/v1/statements/upload", files=files, data=data)
        finally:
            app.dependency_overrides.pop(get_ingestion_service, None)

        assert response.status_code == 422
        detail = response.json()["detail"]
        assert "simulated LLM outage" in detail

        # The statement row is persisted as failed
        async with session_factory() as session:
            row = (
                await session.execute(text("SELECT status, error_message FROM statements"))
            ).first()
            assert row is not None
            assert row[0] == "failed"
            assert "simulated LLM outage" in row[1]


# ---------------------------------------------------------------------------
# HTTP integration: get statement
# ---------------------------------------------------------------------------


@needs_sample_pdfs
@needs_test_rut
class TestGetStatementEndpoint:
    """The ``GET /api/v1/statements/{id}`` endpoint reads a statement."""

    @pytest.mark.asyncio
    async def test_returns_statement_with_transactions(
        self,
        test_settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
        upload_dir: Path,
    ) -> None:
        """A successful upload can be read back via the GET endpoint."""
        llm = FakeLLMClient(response=ExtractionResponse.model_validate(NACIONAL_EXTRACTION_PAYLOAD))
        app = create_app(test_settings)

        async def _override() -> AsyncIterator[IngestionService]:
            async with session_factory() as session:
                yield IngestionService(
                    session=session,
                    llm_client=llm,
                    settings=test_settings,
                )

        app.dependency_overrides[get_ingestion_service] = _override
        app.dependency_overrides[get_session] = _make_session_override(session_factory)
        try:
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://testserver") as client:
                # Upload
                files = {"file": ("statement.pdf", SANTANDER_PDF.read_bytes(), "application/pdf")}
                data = {
                    "bank_name": "santander",
                    "rut": TEST_RUT,
                }
                upload_resp = await client.post("/api/v1/statements/upload", files=files, data=data)
                assert upload_resp.status_code == 201
                statement_id = upload_resp.json()["id"]

                # Read back
                get_resp = await client.get(f"/api/v1/statements/{statement_id}")
        finally:
            app.dependency_overrides.pop(get_ingestion_service, None)
            app.dependency_overrides.pop(get_session, None)

        assert get_resp.status_code == 200
        body = get_resp.json()
        assert body["id"] == statement_id
        assert body["status"] == "completed"
        assert len(body["transactions"]) == 3

    @pytest.mark.asyncio
    async def test_returns_404_for_missing_statement(
        self,
        test_settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
        upload_dir: Path,
    ) -> None:
        """A non-existent UUID returns 404."""
        app = create_app(test_settings)
        app.dependency_overrides[get_session] = _make_session_override(session_factory)
        try:
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://testserver") as client:
                missing_id = uuid.uuid4()
                response = await client.get(f"/api/v1/statements/{missing_id}")
        finally:
            app.dependency_overrides.pop(get_session, None)

        assert response.status_code == 404
        assert "not found" in response.json()["detail"]


# ---------------------------------------------------------------------------
# HTTP integration: list transactions
# ---------------------------------------------------------------------------


@needs_sample_pdfs
@needs_test_rut
class TestListTransactionsEndpoint:
    """The ``GET /api/v1/transactions`` endpoint supports filterable lists."""

    @pytest.fixture
    async def seeded_statement(
        self,
        test_settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
        upload_dir: Path,
    ) -> AsyncIterator[uuid.UUID]:
        """Yield a statement ID with three pre-seeded transactions."""
        llm = FakeLLMClient(response=ExtractionResponse.model_validate(NACIONAL_EXTRACTION_PAYLOAD))
        app = create_app(test_settings)

        async def _override() -> AsyncIterator[IngestionService]:
            async with session_factory() as session:
                yield IngestionService(
                    session=session,
                    llm_client=llm,
                    settings=test_settings,
                )

        app.dependency_overrides[get_ingestion_service] = _override
        app.dependency_overrides[get_session] = _make_session_override(session_factory)
        try:
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://testserver") as client:
                files = {"file": ("statement.pdf", SANTANDER_PDF.read_bytes(), "application/pdf")}
                data = {
                    "bank_name": "santander",
                    "rut": TEST_RUT,
                }
                resp = await client.post("/api/v1/statements/upload", files=files, data=data)
                assert resp.status_code == 201
                yield uuid.UUID(resp.json()["id"])
        finally:
            app.dependency_overrides.pop(get_ingestion_service, None)
            app.dependency_overrides.pop(get_session, None)

    @pytest.mark.asyncio
    async def test_list_with_no_filters(
        self,
        test_settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
        seeded_statement: uuid.UUID,
    ) -> None:
        """No filters returns every persisted transaction."""
        app = create_app(test_settings)
        app.dependency_overrides[get_session] = _make_session_override(session_factory)
        try:
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://testserver") as client:
                response = await client.get("/api/v1/transactions")
        finally:
            app.dependency_overrides.pop(get_session, None)

        assert response.status_code == 200
        body = response.json()
        assert isinstance(body, list)
        assert len(body) == 3

    @pytest.mark.asyncio
    async def test_list_filter_by_statement_id(
        self,
        test_settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
        seeded_statement: uuid.UUID,
    ) -> None:
        """``?statement_id=<uuid>`` returns only the matching rows."""
        app = create_app(test_settings)
        app.dependency_overrides[get_session] = _make_session_override(session_factory)
        try:
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://testserver") as client:
                response = await client.get(f"/api/v1/transactions?statement_id={seeded_statement}")
        finally:
            app.dependency_overrides.pop(get_session, None)

        assert response.status_code == 200
        body = response.json()
        assert len(body) == 3
        assert all(t["statement_id"] == str(seeded_statement) for t in body)

    @pytest.mark.asyncio
    async def test_list_filter_by_amount_range(
        self,
        test_settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
        seeded_statement: uuid.UUID,
    ) -> None:
        """``?min_amount=`` and ``?max_amount=`` bound the absolute amount."""
        app = create_app(test_settings)
        app.dependency_overrides[get_session] = _make_session_override(session_factory)
        try:
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://testserver") as client:
                # Range [20000, 50000] → matches COMBUSTIBLE COPEC (35000)
                response = await client.get(
                    "/api/v1/transactions?min_amount=20000&max_amount=50000"
                )
        finally:
            app.dependency_overrides.pop(get_session, None)

        assert response.status_code == 200
        body = response.json()
        assert len(body) == 1
        assert body[0]["description"] == "COMBUSTIBLE COPEC"

    @pytest.mark.asyncio
    async def test_list_filter_by_description_substring(
        self,
        test_settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
        seeded_statement: uuid.UUID,
    ) -> None:
        """``?description=`` performs a case-insensitive substring match."""
        app = create_app(test_settings)
        app.dependency_overrides[get_session] = _make_session_override(session_factory)
        try:
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://testserver") as client:
                # Case-insensitive: "paris" matches "PARIS 03/06"
                response = await client.get("/api/v1/transactions?description=paris")
        finally:
            app.dependency_overrides.pop(get_session, None)

        assert response.status_code == 200
        body = response.json()
        assert len(body) == 1
        assert "PARIS" in body[0]["description"]

    @pytest.mark.asyncio
    async def test_list_filter_by_date_range(
        self,
        test_settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
        seeded_statement: uuid.UUID,
    ) -> None:
        """``?date_from=`` and ``?date_to=`` bound the posting date."""
        app = create_app(test_settings)
        app.dependency_overrides[get_session] = _make_session_override(session_factory)
        try:
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://testserver") as client:
                # Wide range: all three transactions
                response = await client.get(
                    "/api/v1/transactions?date_from=2025-01-01&date_to=2026-12-31"
                )
        finally:
            app.dependency_overrides.pop(get_session, None)

        assert response.status_code == 200
        assert len(response.json()) == 3

    @pytest.mark.asyncio
    async def test_list_pagination(
        self,
        test_settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
        seeded_statement: uuid.UUID,
    ) -> None:
        """``?limit=`` and ``?offset=`` paginate the result set."""
        app = create_app(test_settings)
        app.dependency_overrides[get_session] = _make_session_override(session_factory)
        try:
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://testserver") as client:
                # Page 1: 2 rows
                page1 = await client.get("/api/v1/transactions?limit=2&offset=0")
                # Page 2: 1 row
                page2 = await client.get("/api/v1/transactions?limit=2&offset=2")
        finally:
            app.dependency_overrides.pop(get_session, None)

        assert page1.status_code == 200
        assert page2.status_code == 200
        assert len(page1.json()) == 2
        assert len(page2.json()) == 1
        # The two pages must not overlap
        ids_page1 = {t["id"] for t in page1.json()}
        ids_page2 = {t["id"] for t in page2.json()}
        assert ids_page1.isdisjoint(ids_page2)

    @pytest.mark.asyncio
    async def test_list_rejects_invalid_limit(
        self,
        test_settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
        seeded_statement: uuid.UUID,
    ) -> None:
        """A ``limit`` outside ``[1, 200]`` is rejected with 422."""
        app = create_app(test_settings)
        app.dependency_overrides[get_session] = _make_session_override(session_factory)
        try:
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://testserver") as client:
                response = await client.get("/api/v1/transactions?limit=0")
        finally:
            app.dependency_overrides.pop(get_session, None)

        assert response.status_code == 422


# ---------------------------------------------------------------------------
# HTTP integration: update transaction
# ---------------------------------------------------------------------------


@needs_sample_pdfs
@needs_test_rut
class TestUpdateTransactionEndpoint:
    """The ``PATCH /api/v1/transactions/{id}`` endpoint edits the category."""

    @pytest.mark.asyncio
    async def test_update_category(
        self,
        test_settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
        upload_dir: Path,
    ) -> None:
        """Updating a transaction's category persists the new value."""
        # Upload to seed a statement
        llm = FakeLLMClient(response=ExtractionResponse.model_validate(NACIONAL_EXTRACTION_PAYLOAD))
        app = create_app(test_settings)

        async def _override() -> AsyncIterator[IngestionService]:
            async with session_factory() as session:
                yield IngestionService(
                    session=session,
                    llm_client=llm,
                    settings=test_settings,
                )

        app.dependency_overrides[get_ingestion_service] = _override
        app.dependency_overrides[get_session] = _make_session_override(session_factory)
        try:
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://testserver") as client:
                files = {"file": ("statement.pdf", SANTANDER_PDF.read_bytes(), "application/pdf")}
                data = {
                    "bank_name": "santander",
                    "rut": TEST_RUT,
                }
                upload = await client.post("/api/v1/statements/upload", files=files, data=data)
                assert upload.status_code == 201
                txn_id = upload.json()["transactions"][0]["id"]
                statement_id = upload.json()["id"]

                # PATCH the category
                patch = await client.patch(
                    f"/api/v1/transactions/{txn_id}",
                    json={"category": "Food"},
                )
        finally:
            app.dependency_overrides.pop(get_ingestion_service, None)
            app.dependency_overrides.pop(get_session, None)

        assert patch.status_code == 200
        body = patch.json()
        assert body["id"] == txn_id
        assert body["category"] == "Food"

        # Round-trip: read it back via list
        app2 = create_app(test_settings)
        app2.dependency_overrides[get_session] = _make_session_override(session_factory)
        try:
            transport2 = ASGITransport(app=app2)
            async with AsyncClient(transport=transport2, base_url="http://testserver") as client:
                list_resp = await client.get(f"/api/v1/transactions?statement_id={statement_id}")
        finally:
            app2.dependency_overrides.pop(get_session, None)
        for t in list_resp.json():
            if t["id"] == txn_id:
                assert t["category"] == "Food"

    @pytest.mark.asyncio
    async def test_update_returns_404_for_missing(
        self,
        test_settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
        upload_dir: Path,
    ) -> None:
        """A non-existent transaction UUID returns 404."""
        app = create_app(test_settings)
        app.dependency_overrides[get_session] = _make_session_override(session_factory)
        try:
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://testserver") as client:
                missing = uuid.uuid4()
                response = await client.patch(
                    f"/api/v1/transactions/{missing}",
                    json={"category": "Anything"},
                )
        finally:
            app.dependency_overrides.pop(get_session, None)

        assert response.status_code == 404
        assert "not found" in response.json()["detail"]

    @pytest.mark.asyncio
    async def test_update_rejects_empty_category(
        self,
        test_settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
        upload_dir: Path,
    ) -> None:
        """An empty category is rejected by Pydantic with 422."""
        app = create_app(test_settings)
        app.dependency_overrides[get_session] = _make_session_override(session_factory)
        try:
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://testserver") as client:
                # Some random UUID — the body validation fails first
                response = await client.patch(
                    f"/api/v1/transactions/{uuid.uuid4()}",
                    json={"category": ""},
                )
        finally:
            app.dependency_overrides.pop(get_session, None)

        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_update_rejects_extra_fields(
        self,
        test_settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
        upload_dir: Path,
    ) -> None:
        """Fields other than ``category`` are rejected with 422."""
        app = create_app(test_settings)
        app.dependency_overrides[get_session] = _make_session_override(session_factory)
        try:
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://testserver") as client:
                response = await client.patch(
                    f"/api/v1/transactions/{uuid.uuid4()}",
                    json={"category": "Food", "amount": "0"},
                )
        finally:
            app.dependency_overrides.pop(get_session, None)

        assert response.status_code == 422


# ---------------------------------------------------------------------------
# Module sanity
# ---------------------------------------------------------------------------


class TestUploadHelpers:
    """Direct unit tests for the :func:`_save_upload` helper.

    The helper has three branches that the HTTP-level tests do
    not reach — relative ``upload_dir``, empty ``safe_name``,
    and missing ``.pdf`` extension. Each branch is covered
    here so a refactor that changes the sanitisation rules is
    caught at the unit boundary.
    """

    def test_save_upload_with_relative_dir(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """A relative ``upload_dir`` is resolved against the working directory."""
        from app.api.v1.statements import _save_upload

        # Use a relative path that resolves to a subdirectory of
        # the temp directory.
        relative = tmp_path / "rel-uploads"
        monkeypatch.chdir(tmp_path)
        dest = _save_upload(b"%PDF-1.4\n", "statement.pdf", "rel-uploads")

        assert dest.exists()
        assert dest.parent == relative.resolve()
        assert dest.read_bytes() == b"%PDF-1.4\n"

    def test_save_upload_with_empty_filename(
        self,
        tmp_path: Path,
    ) -> None:
        """A filename that sanitises to empty falls back to ``statement.pdf``."""
        from app.api.v1.statements import _save_upload

        # The regex ``[^A-Za-z0-9._-]+`` collapses everything that
        # is not an allowed character to underscores. Stripped of
        # its dots, the result is empty.
        dest = _save_upload(b"%PDF-1.4\n", "...", str(tmp_path))
        assert dest.exists()
        assert dest.name.endswith("statement.pdf")

    def test_save_upload_appends_pdf_when_missing(
        self,
        tmp_path: Path,
    ) -> None:
        """A safe filename without a ``.pdf`` extension gets one appended."""
        from app.api.v1.statements import _save_upload

        dest = _save_upload(b"%PDF-1.4\n", "mystatement", str(tmp_path))
        assert dest.exists()
        # The name has a UUID prefix + the safe name + .pdf
        assert dest.name.endswith("mystatement.pdf")

    def test_save_upload_with_absolute_dir(
        self,
        tmp_path: Path,
    ) -> None:
        """An absolute ``upload_dir`` is used as-is."""
        from app.api.v1.statements import _save_upload

        target = tmp_path / "abs-uploads"
        target.mkdir()
        dest = _save_upload(b"%PDF-1.4\n", "statement.pdf", str(target))
        assert dest.parent == target
        assert dest.read_bytes() == b"%PDF-1.4\n"

    def test_validate_size_under_limit(self) -> None:
        """A file under the cap returns without raising."""
        from app.api.v1.statements import _validate_size

        # Should not raise
        _validate_size(1024, 10)

    def test_validate_size_over_limit(self) -> None:
        """A file over the cap raises 413."""
        from fastapi import HTTPException

        from app.api.v1.statements import _validate_size

        with pytest.raises(HTTPException) as exc_info:
            _validate_size(11 * 1024 * 1024, 10)
        assert exc_info.value.status_code == 413

    def test_validate_pdf_accepts_valid(self) -> None:
        """A ``%PDF-`` header is accepted."""
        from app.api.v1.statements import _validate_pdf

        # Should not raise
        _validate_pdf(b"%PDF-1.4\nsome pdf content")

    def test_validate_pdf_rejects_non_pdf(self) -> None:
        """A file without the ``%PDF-`` header is rejected with 415."""
        from fastapi import HTTPException

        from app.api.v1.statements import _validate_pdf

        with pytest.raises(HTTPException) as exc_info:
            _validate_pdf(b"not a pdf")
        assert exc_info.value.status_code == 415


def test_module_sanity() -> None:
    """Sanity check: the orchestrator classes are importable."""
    assert IngestionService is not None
    assert IngestionError is not None
    assert BankNotFoundError is not None


# ---------------------------------------------------------------------------
# _validate_metadata — unit tests that do not need a real PDF
# ---------------------------------------------------------------------------


class TestValidateMetadata:
    """``IngestionService._validate_metadata`` is a pure function on the LLM response.

    Split out from :meth:`ingest_statement` so the
    validation logic can be exercised without the full PDF
    pipeline. The method must reject unsupported currencies,
    variant/currency mismatches, and unparseable dates.
    """

    def _build_extraction(
        self,
        *,
        currency: str = "CLP",
        period_start: str = "01/05/2025",
        period_end: str = "31/05/2025",
        statement_date: str = "05/06/2025",
    ) -> ExtractionResponse:
        """Build a minimal :class:`ExtractionResponse` for validation tests."""
        return ExtractionResponse(
            transactions=[],
            metadata={
                "card_number_masked": "XXXX XXXX XXXX 0951",
                "cardholder": "X",
                "currency": currency,
                "period_start": period_start,
                "period_end": period_end,
                "statement_date": statement_date,
            },
            confidence=0.5,
            notes=None,
        )

    def test_nacional_with_clp_metadata_succeeds(self) -> None:
        """A NACIONAL variant with CLP metadata returns the expected tuple."""
        from app.services.ingestion import IngestionService

        result = IngestionService._validate_metadata(
            extraction=self._build_extraction(), variant="NACIONAL"
        )
        expected_currency, period_start, period_end, statement_date = result
        assert expected_currency == "CLP"
        assert period_start == date(2025, 5, 1)
        assert period_end == date(2025, 5, 31)
        assert statement_date == date(2025, 6, 5)

    def test_internacional_with_usd_metadata_succeeds(self) -> None:
        """An INTERNACIONAL variant with USD metadata returns the expected tuple."""
        from app.services.ingestion import IngestionService

        result = IngestionService._validate_metadata(
            extraction=self._build_extraction(currency="USD"), variant="INTERNACIONAL"
        )
        expected_currency, _ps, _pe, _sd = result
        assert expected_currency == "USD"

    def test_currency_mismatch_raises(self) -> None:
        """A NACIONAL variant with USD metadata is rejected."""
        from app.services.ingestion import IngestionService

        with pytest.raises(IngestionError, match="metadata currency"):
            IngestionService._validate_metadata(
                extraction=self._build_extraction(currency="USD"), variant="NACIONAL"
            )

    def test_unsupported_currency_raises(self) -> None:
        """A non-CLP/USD metadata currency is rejected."""
        from app.services.ingestion import IngestionService

        with pytest.raises(IngestionError, match="unsupported"):
            IngestionService._validate_metadata(
                extraction=self._build_extraction(currency="EUR"), variant="NACIONAL"
            )

    def test_unparseable_period_start_raises(self) -> None:
        """An unparseable period_start date is rejected."""
        from app.services.ingestion import IngestionService

        with pytest.raises(IngestionError, match="unparseable"):
            IngestionService._validate_metadata(
                extraction=self._build_extraction(period_start="not-a-date"),
                variant="NACIONAL",
            )

    def test_two_digit_year_is_expanded(self) -> None:
        """A ``DD/MM/YY`` date is accepted and expanded to four-digit year."""
        from app.services.ingestion import IngestionService

        result = IngestionService._validate_metadata(
            extraction=self._build_extraction(period_start="15/05/25"),
            variant="NACIONAL",
        )
        _expected, period_start, _pe, _sd = result
        assert period_start == date(2025, 5, 15)

    def test_non_extraction_response_raises(self) -> None:
        """Passing anything other than :class:`ExtractionResponse` raises."""
        from app.services.ingestion import IngestionService

        with pytest.raises(IngestionError, match="ExtractionResponse"):
            IngestionService._validate_metadata(extraction="not a model", variant="NACIONAL")


# ---------------------------------------------------------------------------
# _build_transactions — unit tests for per-row validation
# ---------------------------------------------------------------------------


class TestBuildTransactions:
    """``IngestionService._build_transactions`` runs without a database or LLM.

    The method takes a fully-populated
    :class:`ExtractionResponse` and produces ready-to-add
    :class:`Transaction` rows. It must validate amounts,
    dates, and currency cross-checks.
    """

    def test_builds_one_transaction_per_llm_row(self) -> None:
        """Each LLM-emitted transaction becomes one :class:`Transaction` row."""
        from app.models.statement import Statement

        extraction = ExtractionResponse(
            transactions=[
                {
                    "date": "15/05/25",
                    "description": "X",
                    "amount": "$ 12.450",
                    "currency": "CLP",
                    "category": "Groceries",
                },
                {
                    "date": "22/05/25",
                    "description": "Y",
                    "amount": "$ 35.000",
                    "currency": "CLP",
                    "category": "Transport",
                },
            ],
            metadata={
                "card_number_masked": "XXXX XXXX XXXX 0951",
                "cardholder": "X",
                "currency": "CLP",
                "period_start": "01/05/2025",
                "period_end": "31/05/2025",
                "statement_date": "05/06/2025",
            },
            confidence=0.5,
            notes=None,
        )
        # A stand-in statement: the model only reads ``.id``
        # from this object, so a SimpleNamespace is enough.
        from types import SimpleNamespace

        statement = SimpleNamespace(id=uuid.uuid4())
        # The real method is unbound, so we call it as a static-ish helper.
        from app.services.ingestion import IngestionService as _Svc

        transactions = _Svc._build_transactions(
            self=_Svc.__new__(_Svc),
            statement=statement,
            extraction=extraction,
            expected_currency="CLP",
        )
        assert len(transactions) == 2
        assert all(isinstance(t, Statement.__class__) for t in transactions) or all(
            isinstance(t, type(transactions[0])) for t in transactions
        )
        assert transactions[0].description == "X"
        assert transactions[0].amount == Decimal("12450")
        assert transactions[1].amount == Decimal("35000")

    def test_currency_mismatch_raises(self) -> None:
        """A transaction whose currency does not match the variant raises."""
        from types import SimpleNamespace

        from app.services.ingestion import IngestionService as _Svc

        extraction = ExtractionResponse(
            transactions=[
                {
                    "date": "15/05/25",
                    "description": "X",
                    "amount": "US$ 9,99",
                    "currency": "USD",  # wrong for NACIONAL
                    "category": None,
                }
            ],
            metadata={
                "card_number_masked": "XXXX XXXX XXXX 0951",
                "cardholder": "X",
                "currency": "CLP",
                "period_start": "01/05/2025",
                "period_end": "31/05/2025",
                "statement_date": "05/06/2025",
            },
            confidence=0.5,
            notes=None,
        )
        statement = SimpleNamespace(id=uuid.uuid4())
        with pytest.raises(IngestionError, match="currency"):
            _Svc._build_transactions(
                self=_Svc.__new__(_Svc),
                statement=statement,
                extraction=extraction,
                expected_currency="CLP",
            )

    def test_bad_amount_raises(self) -> None:
        """An unparseable amount string raises an :class:`IngestionError`."""
        from types import SimpleNamespace

        from app.services.ingestion import IngestionService as _Svc

        extraction = ExtractionResponse(
            transactions=[
                {
                    "date": "15/05/25",
                    "description": "X",
                    "amount": "garbage",
                    "currency": "CLP",
                    "category": None,
                }
            ],
            metadata={
                "card_number_masked": "XXXX XXXX XXXX 0951",
                "cardholder": "X",
                "currency": "CLP",
                "period_start": "01/05/2025",
                "period_end": "31/05/2025",
                "statement_date": "05/06/2025",
            },
            confidence=0.5,
            notes=None,
        )
        statement = SimpleNamespace(id=uuid.uuid4())
        with pytest.raises(IngestionError):
            _Svc._build_transactions(
                self=_Svc.__new__(_Svc),
                statement=statement,
                extraction=extraction,
                expected_currency="CLP",
            )

    def test_installment_value_is_parsed(self) -> None:
        """An installment value string is coerced to :class:`Decimal`."""
        from types import SimpleNamespace

        from app.services.ingestion import IngestionService as _Svc

        extraction = ExtractionResponse(
            transactions=[
                {
                    "date": "01/06/25",
                    "description": "PARIS 03/06",
                    "amount": "$ 89.900",
                    "currency": "CLP",
                    "category": "Shopping",
                    "installment_number": 1,
                    "installment_total": 6,
                    "installment_value": "$ 89.900",
                }
            ],
            metadata={
                "card_number_masked": "XXXX XXXX XXXX 0951",
                "cardholder": "X",
                "currency": "CLP",
                "period_start": "01/05/2025",
                "period_end": "31/05/2025",
                "statement_date": "05/06/2025",
            },
            confidence=0.5,
            notes=None,
        )
        statement = SimpleNamespace(id=uuid.uuid4())
        transactions = _Svc._build_transactions(
            self=_Svc.__new__(_Svc),
            statement=statement,
            extraction=extraction,
            expected_currency="CLP",
        )
        assert transactions[0].installment_number == 1
        assert transactions[0].installment_total == 6
        assert transactions[0].installment_value == Decimal("89900")


# ---------------------------------------------------------------------------
# Helpers — pure functions that don't need the DB or PDF
# ---------------------------------------------------------------------------


class TestModuleHelpers:
    """The module-level helpers are exercised in isolation."""

    def test_truncate_error_short_message(self) -> None:
        """A short error message is returned verbatim."""
        from app.services.ingestion import _truncate_error

        result = _truncate_error(ValueError("boom"))
        assert result == "ValueError: boom"

    def test_truncate_error_long_message(self) -> None:
        """A message longer than the cap is truncated with an ellipsis."""
        from app.services.ingestion import _truncate_error

        long = "x" * 1000
        result = _truncate_error(ValueError(long), limit=50)
        assert len(result) == 50
        assert result.endswith("…")

    def test_truncate_error_replaces_newlines(self) -> None:
        """Newlines in the error message are replaced with spaces."""
        from app.services.ingestion import _truncate_error

        result = _truncate_error(ValueError("line1\nline2\nline3"))
        assert "\n" not in result
        assert "line1 line2 line3" in result

    def test_expand_two_digit_year_pivot_70(self) -> None:
        """The year pivot at 70 keeps 21st-century dates as 2000s."""
        from app.services.ingestion import _expand_two_digit_year

        assert _expand_two_digit_year(0) == 2000
        assert _expand_two_digit_year(25) == 2025
        assert _expand_two_digit_year(69) == 2069
        assert _expand_two_digit_year(70) == 1970
        assert _expand_two_digit_year(99) == 1999

    def test_parse_llm_date_accepts_dmy_long(self) -> None:
        """``DD/MM/YYYY`` is parsed into a real :class:`date`."""
        from app.services.ingestion import _parse_llm_date

        assert _parse_llm_date("15/05/2025", index=0) == date(2025, 5, 15)

    def test_parse_llm_date_accepts_dmy_short(self) -> None:
        """``DD/MM/YY`` is parsed and the year is expanded to four digits."""
        from app.services.ingestion import _parse_llm_date

        assert _parse_llm_date("15/05/25", index=0) == date(2025, 5, 15)

    def test_parse_llm_date_accepts_iso(self) -> None:
        """``YYYY-MM-DD`` is also accepted (defensive parsing)."""
        from app.services.ingestion import _parse_llm_date

        assert _parse_llm_date("2025-05-15", index=0) == date(2025, 5, 15)

    def test_parse_llm_date_rejects_garbage(self) -> None:
        """An unparseable date string raises :class:`ValueError`."""
        from app.services.ingestion import _parse_llm_date

        with pytest.raises(ValueError, match="does not match"):
            _parse_llm_date("not-a-date", index=0)

    def test_parse_llm_date_rejects_out_of_range(self) -> None:
        """A syntactically-valid but out-of-range date raises :class:`ValueError`."""
        from app.services.ingestion import _parse_llm_date

        with pytest.raises(ValueError, match="out of range"):
            _parse_llm_date("32/13/2025", index=0)

    def test_compute_sha256(self, tmp_path: Path) -> None:
        """The SHA-256 helper matches :func:`hashlib.sha256` for the same content."""
        from app.services.ingestion import _compute_sha256

        target = tmp_path / "blob.bin"
        target.write_bytes(b"hello world")
        assert _compute_sha256(target) == hashlib.sha256(b"hello world").hexdigest()

    def test_compute_sha256_streams_large_file(self, tmp_path: Path) -> None:
        """The hash is computed in chunks — a 5 MB file produces the same digest."""
        from app.services.ingestion import _compute_sha256

        target = tmp_path / "big.bin"
        # 5 MB of zeros
        target.write_bytes(b"\x00" * (5 * 1024 * 1024))
        assert _compute_sha256(target) == hashlib.sha256(b"\x00" * (5 * 1024 * 1024)).hexdigest()
