"""End-to-end test for Phase 1.

Exercises the full ingestion path — upload form → API endpoint →
ingestion orchestrator → database — against a real PDF from
``shared/account-state-examples/``. The LLM is replaced with a
fake so the test stays hermetic and the assertions are
deterministic.

The test is the "smoke test" for Phase 1: a green run means the
upload page, the upload endpoint, the ingestion service, and
the transactions list page all wire together end-to-end with
real PDF input. It is intentionally one big happy-path test
rather than a dozen small ones, because the surface is
narrow: one PDF, three transactions, one list page.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.api.v1.statements import get_ingestion_service
from app.core.config import Settings
from app.db.engine import create_engine
from app.db.session import get_session
from app.main import create_app
from app.models.bank import Bank
from app.models.base import Base
from app.models.statement import Statement, StatementStatus
from app.models.transaction import Transaction
from app.services.ingestion import IngestionService
from app.services.llm.schemas import ExtractionResponse

# ---------------------------------------------------------------------------
# Sample PDFs
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SAMPLE_PDFS_DIR = PROJECT_ROOT / "shared" / "account-state-examples"

SANTANDER_PDF = SAMPLE_PDFS_DIR / "80_15796_0350262800062166708_20260422.pdf"

#: Cardholder RUT, read from the environment so the real identifier
#: never has to be committed. The orchestrator derives the
#: per-bank PDF password from this value, so the happy-path tests
#: below need it. The wrong-RUT test deliberately uses a
#: hard-coded fictional RUT, so it does not.
TEST_RUT: str | None = os.getenv("TEST_RUT")

_SAMPLE_PDF_PRESENT = SANTANDER_PDF.exists()
needs_sample_pdf = pytest.mark.skipif(
    not _SAMPLE_PDF_PRESENT,
    reason=(
        f"Sample PDF not found in {SAMPLE_PDFS_DIR}. The E2E test is skipped in this environment."
    ),
)

#: Skip the E2E tests that decrypt the real sample PDF: they need
#: the cardholder RUT to derive the right password, and that
#: value lives in the env, not in the repo. Set
#: ``TEST_RUT=<your-rut>`` to run them.
needs_test_rut = pytest.mark.skipif(
    TEST_RUT is None,
    reason=(
        "TEST_RUT environment variable not set. "
        "The E2E test that decrypts a real PDF is skipped to keep "
        "the cardholder's RUT out of the repository. Run locally with "
        "`TEST_RUT=<your-rut> pytest tests/test_e2e_phase1.py`."
    ),
)


# ---------------------------------------------------------------------------
# Fake LLM client
# ---------------------------------------------------------------------------


@dataclass
class FakeLLMClient:
    """In-memory :class:`LLMProvider` for the E2E test.

    Returns a canned NACIONAL extraction. The real LLM is not
    exercised; this is the seam the E2E test uses to assert
    "given real PDF + fake LLM, the page shows the extracted
    rows" without paying for or being flaky on a live
    completion.
    """

    response: ExtractionResponse
    calls: list[tuple[str, str]] = field(default_factory=list)

    async def extract_transactions(self, text: str, variant: str) -> ExtractionResponse:
        """Return the canned response and record the call."""
        self.calls.append((text, variant))
        return self.response

    async def aclose(self) -> None:
        """No resources to release."""


CANNED_NACIONAL_EXTRACTION: dict[str, object] = {
    "transactions": [
        {
            "date": "05/04/26",
            "description": "SUPERMERCADOS LIDER",
            "amount": "$ 12.450",
            "currency": "CLP",
            "category": "Groceries",
            "installment_number": None,
            "installment_total": None,
            "installment_value": None,
        },
        {
            "date": "10/04/26",
            "description": "COMBUSTIBLE COPEC",
            "amount": "$ 35.000",
            "currency": "CLP",
            "category": "Transport",
            "installment_number": None,
            "installment_total": None,
            "installment_value": None,
        },
        {
            "date": "15/04/26",
            "description": "PARIS",
            "amount": "$ 89.900",
            "currency": "CLP",
            "category": "Shopping",
            "installment_number": 3,
            "installment_total": 6,
            "installment_value": "$ 89.900",
        },
    ],
    "metadata": {
        "card_number_masked": "XXXX XXXX XXXX 0463",
        "cardholder": "LUIS SOTILLO",
        "currency": "CLP",
        "period_start": "01/04/2026",
        "period_end": "30/04/2026",
        "statement_date": "22/04/2026",
    },
    "confidence": 0.95,
    "notes": "E2E test canned response.",
}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def seeded_engine(test_settings: Settings) -> AsyncIterator[AsyncEngine]:
    """Yield an engine with the full schema and the three banks seeded."""
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
    """Yield a session factory bound to the seeded engine."""
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
# Helpers
# ---------------------------------------------------------------------------


def _make_session_override(
    factory: async_sessionmaker[AsyncSession],
):
    """Return a ``get_session`` override bound to ``factory``."""

    async def _override() -> AsyncIterator[AsyncSession]:
        async with factory() as session:
            yield session

    return _override


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


@needs_sample_pdf
@needs_test_rut
@pytest.mark.asyncio
async def test_upload_real_pdf_creates_completed_statement(
    test_settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    upload_dir: Path,
) -> None:
    """End-to-end: upload a real Santander PDF, get a completed statement."""
    llm = FakeLLMClient(response=ExtractionResponse.model_validate(CANNED_NACIONAL_EXTRACTION))
    app = create_app(test_settings)

    async def _ingestion_override() -> AsyncIterator[IngestionService]:
        async with session_factory() as session:
            yield IngestionService(
                session=session,
                llm_client=llm,
                settings=test_settings,
            )

    async def _session_override() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_ingestion_service] = _ingestion_override
    app.dependency_overrides[get_session] = _session_override
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
        app.dependency_overrides.pop(get_session, None)

    # 1. The upload endpoint returned 201 with a completed statement
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == StatementStatus.COMPLETED.value
    assert len(body["transactions"]) == 3

    # 2. The LLM was called once with the NACIONAL variant
    assert len(llm.calls) == 1
    _text, variant = llm.calls[0]
    assert variant == "NACIONAL"

    # 3. The statement row is persisted with the parsed transactions
    statement_id = uuid.UUID(body["id"])
    async with session_factory() as session:
        result = await session.execute(select(Statement).where(Statement.id == statement_id))
        statement = result.scalar_one()
        assert statement.status == StatementStatus.COMPLETED
        assert statement.error_message is None
        assert statement.credit_card is not None
        assert statement.credit_card.bank.name == "santander"
        assert statement.credit_card.cardholder == "LUIS SOTILLO"
        # Transactions are persisted as Decimal — the orchestrator
        # parses the LLM's "$ 12.450" string into a Decimal
        # amount.
        assert len(statement.transactions) == 3
        by_desc = {t.description: t.amount for t in statement.transactions}
        assert by_desc["SUPERMERCADOS LIDER"] == Decimal("12450")
        assert by_desc["COMBUSTIBLE COPEC"] == Decimal("35000")
        assert by_desc["PARIS"] == Decimal("89900")

    # 4. The transactions list page renders with the rows
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        list_response = await client.get("/transactions")
    assert list_response.status_code == 200
    list_body = list_response.text
    assert "SUPERMERCADOS LIDER" in list_body
    assert "COMBUSTIBLE COPEC" in list_body
    assert "PARIS" in list_body
    assert "3 transaction(s)" in list_body

    # 5. The transactions page filtered to the statement shows the rows too
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        filtered = await client.get("/transactions", params={"statement_id": str(statement_id)})
    assert filtered.status_code == 200
    assert "PARIS" in filtered.text

    # 6. The HTMX partial returns the same rows
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        rows_response = await client.get("/transactions/rows")
    assert rows_response.status_code == 200
    assert "SUPERMERCADOS LIDER" in rows_response.text

    # 7. The upload page itself still renders after the upload
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        page_response = await client.get("/upload")
    assert page_response.status_code == 200
    page_body = page_response.text
    assert "Upload statement" in page_body
    # The bank dropdown is still populated
    assert "Banco Santander" in page_body


# ---------------------------------------------------------------------------
# Failure modes flow through to the UI
# ---------------------------------------------------------------------------


@needs_sample_pdf
@pytest.mark.asyncio
async def test_wrong_rut_returns_422_without_statement(
    test_settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    upload_dir: Path,
) -> None:
    """A wrong RUT decrypt fails → endpoint returns 422 with no statement row.

    The E2E test confirms the failure mode is observable: a bad
    RUT (which yields a wrong PDF password) makes the pipeline
    fail and the upload endpoint returns 422 with a useful
    error message. No statement row is created — pre-LLM
    errors are fast-fail so the user can retry with a
    corrected RUT without leaving a partial artifact behind.
    """
    from app.services.llm.schemas import ExtractionResponse

    llm = FakeLLMClient(response=ExtractionResponse.model_validate(CANNED_NACIONAL_EXTRACTION))
    app = create_app(test_settings)

    async def _ingestion_override() -> AsyncIterator[IngestionService]:
        async with session_factory() as session:
            yield IngestionService(
                session=session,
                llm_client=llm,
                settings=test_settings,
            )

    app.dependency_overrides[get_ingestion_service] = _ingestion_override
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            files = {"file": ("statement.pdf", SANTANDER_PDF.read_bytes(), "application/pdf")}
            data = {
                "bank_name": "santander",
                "rut": "11.111.111-1",  # wrong RUT → wrong password
            }
            response = await client.post("/api/v1/statements/upload", files=files, data=data)
    finally:
        app.dependency_overrides.pop(get_ingestion_service, None)

    # The endpoint returns 422 with the failure reason
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert "password" in detail.lower()

    # No statement row was created — pre-LLM errors are fast-fail
    async with session_factory() as session:
        result = await session.execute(select(Statement))
        statements = list(result.scalars().all())
        assert len(statements) == 0


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


@needs_sample_pdf
@needs_test_rut
@pytest.mark.asyncio
async def test_duplicate_upload_does_not_create_extra_transactions(
    test_settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    upload_dir: Path,
) -> None:
    """Uploading the same PDF twice does not duplicate transactions."""
    llm = FakeLLMClient(response=ExtractionResponse.model_validate(CANNED_NACIONAL_EXTRACTION))
    app = create_app(test_settings)

    async def _ingestion_override() -> AsyncIterator[IngestionService]:
        async with session_factory() as session:
            yield IngestionService(
                session=session,
                llm_client=llm,
                settings=test_settings,
            )

    app.dependency_overrides[get_ingestion_service] = _ingestion_override
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            files = {"file": ("statement.pdf", SANTANDER_PDF.read_bytes(), "application/pdf")}
            data = {
                "bank_name": "santander",
                "rut": TEST_RUT,
            }
            first = await client.post("/api/v1/statements/upload", files=files, data=data)
            second = await client.post("/api/v1/statements/upload", files=files, data=data)
    finally:
        app.dependency_overrides.pop(get_ingestion_service, None)

    # Both responses are 201 with the same statement ID and same row count
    assert first.status_code == 201
    assert second.status_code == 201
    first_id = first.json()["id"]
    second_id = second.json()["id"]
    assert first_id == second_id

    # The LLM was only called once — the second upload was deduped
    assert len(llm.calls) == 1

    # The database has exactly one statement with three transactions
    async with session_factory() as session:
        result = await session.execute(select(Statement))
        statements = list(result.scalars().all())
        assert len(statements) == 1
        result = await session.execute(select(Transaction))
        transactions = list(result.scalars().all())
        assert len(transactions) == 3


# ---------------------------------------------------------------------------
# Category editing via the API (HTMX PATCH target on the list page)
# ---------------------------------------------------------------------------


@needs_sample_pdf
@needs_test_rut
@pytest.mark.asyncio
async def test_patch_category_persists(
    test_settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    upload_dir: Path,
) -> None:
    """The category PATCH endpoint (HTMX target on the list page) persists."""
    llm = FakeLLMClient(response=ExtractionResponse.model_validate(CANNED_NACIONAL_EXTRACTION))
    app = create_app(test_settings)

    async def _ingestion_override() -> AsyncIterator[IngestionService]:
        async with session_factory() as session:
            yield IngestionService(
                session=session,
                llm_client=llm,
                settings=test_settings,
            )

    async def _session_override() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_ingestion_service] = _ingestion_override
    app.dependency_overrides[get_session] = _session_override
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            # 1. Upload
            files = {"file": ("statement.pdf", SANTANDER_PDF.read_bytes(), "application/pdf")}
            data = {
                "bank_name": "santander",
                "rut": TEST_RUT,
            }
            upload_resp = await client.post("/api/v1/statements/upload", files=files, data=data)
            assert upload_resp.status_code == 201
            statement = upload_resp.json()

            # 2. Pick a transaction and PATCH its category
            txn_id = statement["transactions"][0]["id"]
            patch_resp = await client.patch(
                f"/api/v1/transactions/{txn_id}",
                data={"category": "Groceries"},
            )
    finally:
        app.dependency_overrides.pop(get_ingestion_service, None)
        app.dependency_overrides.pop(get_session, None)

    assert patch_resp.status_code == 200
    assert patch_resp.json()["category"] == "Groceries"

    # 3. The list page reflects the new category on the next request
    app = create_app(test_settings)
    app.dependency_overrides[get_session] = _session_override
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            list_resp = await client.get("/transactions")
    finally:
        app.dependency_overrides.pop(get_session, None)

    assert "Groceries" in list_resp.text
