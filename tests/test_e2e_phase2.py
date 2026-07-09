"""End-to-end test for Phase 2.

Exercises the Phase 2 happy path on the real Santander PDF:
upload → ingestion populates ``merchant_id``, ``category_id``,
and ``recurring_rule_id`` on each transaction → the recurring
detector creates a LIDER rule (the canned 3rd LIDER row trips
the 3-occurrence threshold when combined with the two pre-seeded
historical LIDER transactions) → the user toggles the rule via
``PATCH /api/v1/recurring/{id}`` and the FK on the new
statement's LIDER transaction is preserved (design D).

Mirrors :mod:`tests.test_e2e_phase1` (same ``FakeLLMClient``,
same ``CANNED_NACIONAL_EXTRACTION``, same ``SANTANDER_PDF``,
same ``needs_sample_pdf`` + ``needs_test_rut`` markers, same
``ASGITransport`` + ``httpx.AsyncClient`` pattern). One big
happy-path test rather than a dozen small ones, because the
Phase 2 surface is narrow: one PDF, three transactions, one
LIDER pattern, and the four read/write endpoints that consume
them. A green run means the categories rename endpoint, the
merchants list endpoint, the recurring detector, and the
recurring PATCH endpoint all wire together against real PDF
input — the "smoke test" for Phase 2.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
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
from app.models import (
    Bank,
    Category,
    CreditCard,
    Merchant,
    MerchantAlias,
    RecurringRule,
    Statement,
    Transaction,
)
from app.models.base import Base
from app.models.merchant import MerchantAliasSource
from app.models.statement import StatementStatus
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
#: below need it.
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
        "`TEST_RUT=<your-rut> pytest tests/test_e2e_phase2.py`."
    ),
)


# ---------------------------------------------------------------------------
# Fake LLM client
# ---------------------------------------------------------------------------


@dataclass
class FakeLLMClient:
    """In-memory :class:`LLMProvider` for the E2E test.

    Returns the same canned NACIONAL extraction
    :mod:`tests.test_e2e_phase1` uses — three rows
    (SUPERMERCADOS LIDER, COMBUSTIBLE COPEC, PARIS) and the
    standard metadata block. The LLM is replaced with this
    fake so the test stays hermetic and the detector's
    behavior is deterministic.
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
            "category": "Transportation",
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
    "notes": "E2E phase 2 canned response.",
}


# ---------------------------------------------------------------------------
# Phase 2 historical anchor
# ---------------------------------------------------------------------------

#: The canned LIDER row in ``CANNED_NACIONAL_EXTRACTION`` is
#: dated ``"05/04/26"`` = **2026-04-05** (NOT 2026-04-15, which
#: is the PARIS row). The detector scans the 90 days before the
#: new statement's period_end (2026-04-30 → cutoff 2026-01-30),
#: so the pre-seeded historical LIDER transactions at -60d and
#: -30d from the canned row all fall inside the window.
#: Intervals are 30 days each, so the detector classifies the
#: cadence as ``monthly``.
LIDER_CANNED_DATE = date(2026, 4, 5)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def seeded_engine(test_settings: Settings) -> AsyncIterator[AsyncEngine]:
    """Yield an engine with the full schema, the three banks, and the 12 categories.

    The Phase 2 e2e needs the closed-set :class:`Category` seed
    so the LLM-emitted category strings resolve to a
    ``category_id`` FK during the upload (the ingestion layer
    does a one-query + in-memory dict lookup against the seed).
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

        async with factory() as session:
            seed = (
                ("Dining Out", "Dining Out", 1),
                ("Groceries", "Groceries", 2),
                ("Transportation", "Transportation", 3),
                ("Shopping", "Shopping", 4),
                ("Entertainment", "Entertainment", 5),
                ("Bills", "Bills & Utilities", 6),
                ("Health", "Health & Medical", 7),
                ("Travel", "Travel", 8),
                ("Subscriptions", "Subscriptions", 9),
                ("Personal Care", "Personal Care", 10),
                ("Uncategorized", "Uncategorized", 11),
                ("Other", "Other", 12),
            )
            for name, display, order in seed:
                session.add(
                    Category(
                        name=name,
                        display_name=display,
                        sort_order=order,
                    )
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
async def phase2_world(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[dict[str, object]]:
    """Yield the IDs the happy-path test needs.

    Builds the seed world: 1 Santander card matching the canned
    LLM metadata, 1 LIDER merchant with a ``SUPERMERCADOS LIDER``
    alias (``source='auto'``, ``normalized="lider"``) so the
    canned row hits the alias table, and 1 historical statement
    with 2 historical LIDER transactions on the same
    ``credit_card_id`` dated 60 and 30 days back from the
    canned LIDER row (2026-04-05). The historical rows push the
    detector's occurrence count over the 3-row threshold when
    the canned 3rd LIDER row arrives via the upload.
    """
    groceries_id: uuid.UUID
    lider_id: uuid.UUID
    card_id: uuid.UUID

    async with session_factory() as session:
        groceries_result = await session.execute(
            select(Category).where(Category.name == "Groceries")
        )
        groceries_id = groceries_result.scalar_one().id

        santander_result = await session.execute(select(Bank).where(Bank.name == "santander"))
        santander = santander_result.scalar_one()

        card = CreditCard(
            bank_id=santander.id,
            card_number_masked="XXXX XXXX XXXX 0463",
            cardholder="LUIS SOTILLO",
            currency="CLP",
            is_active=True,
        )
        session.add(card)
        await session.commit()
        await session.refresh(card)
        card_id = card.id

        lider = Merchant(
            name="lider",
            default_category_id=groceries_id,
            is_active=True,
        )
        session.add(lider)
        await session.commit()
        await session.refresh(lider)
        lider_id = lider.id

        alias = MerchantAlias(
            merchant_id=lider_id,
            alias_text="SUPERMERCADOS LIDER",
            normalized="lider",
            source=MerchantAliasSource.AUTO,
            confidence=None,
        )
        session.add(alias)
        await session.commit()

        # Historical statement + 2 LIDER transactions at the
        # same amount as the canned row ($12.450 CLP) so the
        # in-band range collapses to a single value and the
        # detector's median calculation is deterministic.
        historical_statement = Statement(
            credit_card_id=card_id,
            period_start=date(2026, 1, 1),
            period_end=date(2026, 3, 31),
            statement_date=date(2026, 3, 31),
            file_path="historical/lider-seed.pdf",
            file_hash="c" * 64,
            status=StatementStatus.COMPLETED,
        )
        session.add(historical_statement)
        await session.commit()
        await session.refresh(historical_statement)

        for offset_days in (60, 30):
            txn = Transaction(
                statement_id=historical_statement.id,
                date=LIDER_CANNED_DATE - timedelta(days=offset_days),
                description="SUPERMERCADOS LIDER",
                amount=Decimal("12450"),
                currency="CLP",
                category="Groceries",
                category_id=groceries_id,
                low_confidence=False,
            )
            txn.merchant_id = lider_id
            session.add(txn)
        await session.commit()

    yield {
        "card_id": card_id,
        "lider_id": lider_id,
        "groceries_id": groceries_id,
    }


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
# Happy path
# ---------------------------------------------------------------------------


@needs_sample_pdf
@needs_test_rut
@pytest.mark.asyncio
async def test_phase2_happy_path_end_to_end(
    test_settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    upload_dir: Path,
    phase2_world: dict[str, object],
) -> None:
    """End-to-end: upload a real Santander PDF, exercise the Phase 2 read/write endpoints.

    Pre-seeded LIDER history + canned 3rd LIDER row → detector
    creates a monthly rule. The test then drives every Phase 2
    read/write endpoint in order: 3 FKs populated on the new
    transactions, ``GET /api/v1/recurring`` returns the LIDER
    rule with ``period_label="monthly"``, ``POST
    /api/v1/categories/{id}`` rename returns 200,
    ``GET /api/v1/merchants`` returns the LIDER merchant,
    ``PATCH /api/v1/recurring/{id}`` ``is_active=false`` returns
    200, subsequent ``GET`` excludes the rule, and the FK on the
    LIDER transaction is preserved on deactivation (design D).
    """
    lider_id: uuid.UUID = phase2_world["lider_id"]  # type: ignore[assignment]
    groceries_id: uuid.UUID = phase2_world["groceries_id"]  # type: ignore[assignment]

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
            # Upload the real Santander PDF → 201 + 3 transactions.
            files = {"file": ("statement.pdf", SANTANDER_PDF.read_bytes(), "application/pdf")}
            data = {"bank_name": "santander", "rut": TEST_RUT}
            upload_resp = await client.post("/api/v1/statements/upload", files=files, data=data)
            assert upload_resp.status_code == 201, upload_resp.text
            body = upload_resp.json()
            assert body["status"] == StatementStatus.COMPLETED.value
            assert len(body["transactions"]) == 3

            # The LLM was called once with the NACIONAL variant.
            assert len(llm.calls) == 1
            _text, variant = llm.calls[0]
            assert variant == "NACIONAL"

            # Read the 3 new transactions back from the DB and
            # assert the three Phase 2 FKs are populated.
            # ``merchant_id`` and ``category_id`` come from
            # the ingestion layer; ``recurring_rule_id`` on
            # the LIDER row comes from the detector's FK
            # backfill in the same commit.
            statement_id = uuid.UUID(body["id"])
            async with session_factory() as session:
                txns = (
                    (
                        await session.execute(
                            select(Transaction).where(Transaction.statement_id == statement_id)
                        )
                    )
                    .scalars()
                    .all()
                )
            by_desc = {t.description: t for t in txns}
            lider_txn = by_desc["SUPERMERCADOS LIDER"]
            copec_txn = by_desc["COMBUSTIBLE COPEC"]
            paris_txn = by_desc["PARIS"]

            for txn in (lider_txn, copec_txn, paris_txn):
                assert txn.merchant_id is not None
                assert txn.category_id is not None
            assert lider_txn.merchant_id == lider_id
            assert lider_txn.category_id == groceries_id
            assert lider_txn.category == "Groceries"
            assert copec_txn.category == "Transportation"
            assert paris_txn.category == "Shopping"
            # The detector back-filled the FK on the LIDER row only.
            assert lider_txn.recurring_rule_id is not None
            assert copec_txn.recurring_rule_id is None
            assert paris_txn.recurring_rule_id is None
            rule_id = lider_txn.recurring_rule_id

            # GET /api/v1/recurring returns the LIDER rule, monthly.
            list_resp = await client.get("/api/v1/recurring")
            assert list_resp.status_code == 200
            rules = list_resp.json()
            assert len(rules) == 1
            rule = rules[0]
            assert rule["id"] == str(rule_id)
            assert rule["period_label"] == "monthly"
            assert rule["period_days"] == 30
            assert rule["occurrences"] >= 3
            assert rule["confidence"] >= 0.0
            assert rule["is_active"] is True
            assert rule["merchant_id"] == str(lider_id)

            # POST /api/v1/categories/{id} rename (Phase 2 smoke test).
            rename_resp = await client.post(
                f"/api/v1/categories/{groceries_id}",
                json={"display_name": "Supermercados"},
            )
            assert rename_resp.status_code == 200, rename_resp.text
            renamed = rename_resp.json()
            assert renamed["id"] == str(groceries_id)
            assert renamed["display_name"] == "Supermercados"

            # GET /api/v1/merchants returns the LIDER merchant.
            merchants_resp = await client.get("/api/v1/merchants")
            assert merchants_resp.status_code == 200
            merchants = merchants_resp.json()
            merchant_names = [m["name"] for m in merchants]
            assert "lider" in merchant_names
            lider_row = next(m for m in merchants if m["name"] == "lider")
            assert lider_row["id"] == str(lider_id)
            assert lider_row["is_active"] is True

            # PATCH /api/v1/recurring/{id} is_active=false → 200.
            patch_resp = await client.patch(
                f"/api/v1/recurring/{rule_id}",
                json={"is_active": False},
            )
            assert patch_resp.status_code == 200, patch_resp.text
            patched = patch_resp.json()
            assert patched["id"] == str(rule_id)
            assert patched["is_active"] is False

            # Subsequent GET excludes the deactivated rule.
            after_resp = await client.get("/api/v1/recurring")
            assert after_resp.status_code == 200
            assert after_resp.json() == []

            # Design D preservation: the FK on the LIDER
            # transaction is NOT cleared by the deactivation.
            # The PATCH endpoint only flips the visibility
            # flag on the rule itself — the historical link
            # survives for audit.
            async with session_factory() as session:
                txn_after = await session.get(Transaction, lider_txn.id)
                assert txn_after is not None
                assert txn_after.recurring_rule_id == rule_id, (
                    "Design D: deactivation must NOT clear the FK"
                )
                rule_after = await session.get(RecurringRule, rule_id)
                assert rule_after is not None
                assert rule_after.is_active is False
                assert rule_after.merchant_id == lider_id
    finally:
        app.dependency_overrides.pop(get_ingestion_service, None)
        app.dependency_overrides.pop(get_session, None)
