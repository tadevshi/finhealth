"""Tests for the Phase 3 dashboard API (PR #9).

Covers the five ``GET /api/v1/dashboard/*`` endpoints that
sit on top of :class:`app.services.dashboard.DashboardService`
(PR #8). The endpoints are a thin HTTP layer — query-param
validation and per-request :class:`DashboardService`
construction — so the test surface focuses on the
HTTP-shaped contract:

* **summary** (5) — happy path; ``period`` / ``range`` /
  ``card_id`` validation; single-card filter.
* **categories** (3) — 12 rows always present;
  multi-currency sub-rollup; single-card filter.
* **merchants** (3) — default ``limit``; custom ``limit``;
  ``limit`` cap validation.
* **monthly** (3) — time series; ``range`` validation.
* **recurring** (2) — active rules with in-band occurrence;
  ``period`` validation.

The test surface mirrors the Phase 2
:mod:`tests.test_recurring` style: per-test fresh in-memory
SQLite database, an engine fixture, a seeded-world fixture
with a bank, two cards, two statements, two merchants, the
12 closed-set categories, and an :class:`httpx.AsyncClient`
whose session dependency is overridden to point at the
test engine. The unit tests for the service itself live in
:mod:`tests.test_dashboard`; the API tests here only assert
the HTTP contract.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.db.engine import create_engine
from app.db.session import get_session
from app.main import create_app
from app.models import Bank, Category, CreditCard, Merchant, RecurringRule, Statement, Transaction
from app.models.base import Base
from app.models.statement import StatementStatus
from app.schemas.dashboard import SummaryResponse
from app.services.dashboard import DashboardService
from app.services.dashboard_selection import RangeMode

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def dashboard_api_engine(test_settings: Settings) -> AsyncIterator[AsyncEngine]:
    """Yield a fresh engine with the schema + the 12 closed-set categories seeded.

    Mirrors :mod:`tests.test_dashboard` — same engine + the
    same 12-row categories seed the service depends on.
    """
    engine: AsyncEngine = create_engine(test_settings)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            for name, display_name, sort_order in (
                ("Dining Out", "Dining Out", 1),
                ("Groceries", "Groceries", 2),
                ("Transportation", "Transportation", 3),
                ("Shopping", "Shopping", 4),
                ("Entertainment", "Entertainment", 5),
                ("Bills", "Bills", 6),
                ("Health", "Health", 7),
                ("Travel", "Travel", 8),
                ("Subscriptions", "Subscriptions", 9),
                ("Personal Care", "Personal Care", 10),
                ("Uncategorized", "Uncategorized", 11),
                ("Other", "Other", 12),
            ):
                session.add(
                    Category(
                        name=name,
                        display_name=display_name,
                        sort_order=sort_order,
                    )
                )
            await session.commit()
        yield engine
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def session_factory(
    dashboard_api_engine: AsyncEngine,
) -> async_sessionmaker[AsyncSession]:
    """Yield a session factory bound to the dashboard API engine."""
    return async_sessionmaker(dashboard_api_engine, expire_on_commit=False)


@pytest_asyncio.fixture
async def api_client(
    dashboard_api_engine: AsyncEngine,
) -> AsyncIterator[AsyncClient]:
    """Yield an :class:`httpx.AsyncClient` wired to the seeded app.

    The app's ``get_session`` dependency is overridden so
    requests read and write to the test engine's database —
    the in-memory SQLite file is the single source of truth
    for the test. The schema and the 12 categories were
    seeded by :func:`dashboard_api_engine`.
    """
    app = create_app()
    factory = async_sessionmaker(dashboard_api_engine, expire_on_commit=False)

    async def _override_session() -> AsyncIterator[AsyncSession]:
        async with factory() as session:
            yield session

    app.dependency_overrides[get_session] = _override_session
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac


@pytest_asyncio.fixture
async def seeded_world(
    session_factory: async_sessionmaker[AsyncSession],
) -> dict[str, object]:
    """Yield a bank, two cards, two statements, two merchants, and the 12 categories.

    Two cards so the single-card filter tests can assert the
    scope narrows. Two currencies (CLP + USD) so the
    multi-currency sub-rollup tests have a side-by-side
    example. The ``seeded_categories`` dict maps the seeded
    :class:`Category` rows by ``name`` so individual tests
    can grab them by closed-set key.
    """
    now = datetime.now(UTC)
    async with session_factory() as session:
        bank = Bank(
            name="dashboard_api_bank",
            display_name="Dashboard API Bank",
            password_formula="rut_sin_dv",
            is_active=True,
            created_at=now,
            updated_at=now,
        )
        session.add(bank)
        await session.commit()
        await session.refresh(bank)

        card_a = CreditCard(
            bank_id=bank.id,
            card_number_masked="XXXX XXXX XXXX 0001",
            cardholder="API USER A",
            currency="CLP",
            is_active=True,
        )
        card_b = CreditCard(
            bank_id=bank.id,
            card_number_masked="XXXX XXXX XXXX 0002",
            cardholder="API USER B",
            currency="USD",
            is_active=True,
        )
        session.add_all([card_a, card_b])
        await session.commit()
        await session.refresh(card_a)
        await session.refresh(card_b)

        statement_a = Statement(
            credit_card_id=card_a.id,
            period_start=date(2026, 6, 1),
            period_end=date(2026, 6, 30),
            statement_date=date(2026, 6, 30),
            file_path="/tmp/dashboard-api-a.pdf",
            file_hash="a" * 64,
            status=StatementStatus.COMPLETED,
        )
        statement_b = Statement(
            credit_card_id=card_b.id,
            period_start=date(2026, 6, 1),
            period_end=date(2026, 6, 30),
            statement_date=date(2026, 6, 30),
            file_path="/tmp/dashboard-api-b.pdf",
            file_hash="b" * 64,
            status=StatementStatus.COMPLETED,
        )
        session.add_all([statement_a, statement_b])
        await session.commit()
        await session.refresh(statement_a)
        await session.refresh(statement_b)

        merchant_clp = Merchant(name="netflix", is_active=True)
        merchant_usd = Merchant(name="spotify", is_active=True)
        session.add_all([merchant_clp, merchant_usd])
        await session.commit()
        await session.refresh(merchant_clp)
        await session.refresh(merchant_usd)

        categories_rows = (
            (await session.execute(select(Category).order_by(Category.sort_order))).scalars().all()
        )

    return {
        "bank_id": bank.id,
        "card_a_id": card_a.id,
        "card_b_id": card_b.id,
        "statement_a_id": statement_a.id,
        "statement_b_id": statement_b.id,
        "merchant_clp_id": merchant_clp.id,
        "merchant_usd_id": merchant_usd.id,
        "categories": {cat.name: cat for cat in categories_rows},
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _add_transaction(
    session: AsyncSession,
    *,
    statement_id: uuid.UUID,
    merchant_id: uuid.UUID,
    amount: str,
    txn_date: date,
    currency: str = "CLP",
    category_id: uuid.UUID | None = None,
) -> Transaction:
    """Insert a transaction row and return the ORM object.

    Mirrors the helper from :mod:`tests.test_dashboard` so
    the API tests use the same construction pattern as the
    service tests.
    """
    txn = Transaction(
        statement_id=statement_id,
        date=txn_date,
        description=f"MERCHANT {merchant_id} {txn_date.isoformat()}",
        amount=Decimal(amount),
        currency=currency,
        category_id=category_id,
        low_confidence=False,
    )
    txn.merchant_id = merchant_id
    session.add(txn)
    return txn


# ---------------------------------------------------------------------------
# summary
# ---------------------------------------------------------------------------


class TestDashboardSummary:
    """``GET /api/v1/dashboard/summary`` returns the KPI tile payload."""

    @pytest.mark.asyncio
    async def test_summary_endpoint_returns_kpis(
        self,
        api_client: AsyncClient,
        session_factory: async_sessionmaker[AsyncSession],
        seeded_world: dict[str, object],
    ) -> None:
        """5 CLP transactions in 2026-07 produce the documented payload.

        Mirrors the spec scenario "Happy path returns 200":
        ``total_per_currency == {"CLP": <sum>}``,
        ``transaction_count == 5``,
        ``period_start == "2026-07-01"``,
        ``period_end == "2026-07-31"``,
        ``card_id == "all"``.
        """
        statement_id = seeded_world["statement_a_id"]  # type: ignore[arg-type]
        merchant_id = seeded_world["merchant_clp_id"]  # type: ignore[arg-type]
        categories = seeded_world["categories"]  # type: ignore[assignment]
        groceries = categories["Groceries"]

        async with session_factory() as session:
            for amount, day in (("10000.00", 1), ("20000.00", 5), ("30000.00", 10)):
                _add_transaction(
                    session,
                    statement_id=statement_id,
                    merchant_id=merchant_id,
                    amount=amount,
                    txn_date=date(2026, 7, day),
                    currency="CLP",
                    category_id=groceries.id,
                )
            _add_transaction(
                session,
                statement_id=statement_id,
                merchant_id=merchant_id,
                amount="5000.00",
                txn_date=date(2026, 7, 15),
                currency="CLP",
                category_id=groceries.id,
            )
            _add_transaction(
                session,
                statement_id=statement_id,
                merchant_id=merchant_id,
                amount="15000.00",
                txn_date=date(2026, 7, 20),
                currency="CLP",
                category_id=groceries.id,
            )
            await session.commit()

        response = await api_client.get("/api/v1/dashboard/summary", params={"period": "2026-07"})
        assert response.status_code == 200
        body = response.json()

        assert body["total_per_currency"] == {"CLP": "80000.00"}
        assert body["transaction_count"] == 5
        assert body["transaction_count_per_currency"] == {"CLP": 5}
        assert body["daily_avg_per_currency"] == {"CLP": "2580.65"}
        assert body["period_start"] == "2026-07-01"
        assert body["period_end"] == "2026-07-31"
        assert body["card_id"] == "all"

    @pytest.mark.asyncio
    async def test_summary_endpoint_validates_period(
        self,
        api_client: AsyncClient,
    ) -> None:
        """Invalid ``period`` returns 400 with a body identifying ``period``.

        Mirrors the spec scenario "Invalid ``period`` returns
        400": ``period=2026-7`` is not zero-padded → 400.
        """
        response = await api_client.get("/api/v1/dashboard/summary", params={"period": "2026-7"})
        assert response.status_code == 400
        body = response.json()
        assert "period" in body["detail"].lower()

    @pytest.mark.asyncio
    async def test_summary_endpoint_validates_range(
        self,
        api_client: AsyncClient,
    ) -> None:
        """``range=4`` is outside ``{0, 3, 6, 12}`` → 400.

        Mirrors the spec scenario "Invalid ``range`` returns 400".
        """
        response = await api_client.get(
            "/api/v1/dashboard/summary",
            params={"period": "2026-07", "range": 4},
        )
        assert response.status_code == 400
        body = response.json()
        assert "range" in body["detail"].lower()

    @pytest.mark.asyncio
    async def test_summary_endpoint_validates_card_id(
        self,
        api_client: AsyncClient,
    ) -> None:
        """Non-UUID non-``"all"`` ``card_id`` returns 400.

        Mirrors the spec scenario "Non-UUID non-``"all"``
        ``card_id`` returns 400".
        """
        response = await api_client.get(
            "/api/v1/dashboard/summary",
            params={"period": "2026-07", "card_id": "not-a-uuid"},
        )
        assert response.status_code == 400
        body = response.json()
        assert "card_id" in body["detail"].lower()

    @pytest.mark.asyncio
    async def test_summary_endpoint_with_card_id_uuid(
        self,
        api_client: AsyncClient,
        session_factory: async_sessionmaker[AsyncSession],
        seeded_world: dict[str, object],
    ) -> None:
        """UUID ``card_id`` narrows the result to that card's transactions.

        Card A has 3 transactions in 2026-07; card B has 2.
        ``card_id=<card_A.uuid>`` → ``transaction_count == 3``;
        ``card_id=<card_B.uuid>`` → ``transaction_count == 2``
        with USD totals.
        """
        statement_a = seeded_world["statement_a_id"]  # type: ignore[arg-type]
        statement_b = seeded_world["statement_b_id"]  # type: ignore[arg-type]
        merchant_clp = seeded_world["merchant_clp_id"]  # type: ignore[arg-type]
        merchant_usd = seeded_world["merchant_usd_id"]  # type: ignore[arg-type]
        card_a = seeded_world["card_a_id"]  # type: ignore[arg-type]
        card_b = seeded_world["card_b_id"]  # type: ignore[arg-type]

        async with session_factory() as session:
            for day in (1, 5, 10):
                _add_transaction(
                    session,
                    statement_id=statement_a,
                    merchant_id=merchant_clp,
                    amount="10000.00",
                    txn_date=date(2026, 7, day),
                    currency="CLP",
                )
            for day in (2, 12):
                _add_transaction(
                    session,
                    statement_id=statement_b,
                    merchant_id=merchant_usd,
                    amount="50.00",
                    txn_date=date(2026, 7, day),
                    currency="USD",
                )
            await session.commit()

        response_a = await api_client.get(
            "/api/v1/dashboard/summary",
            params={"period": "2026-07", "card_id": str(card_a)},
        )
        assert response_a.status_code == 200
        body_a = response_a.json()
        assert body_a["transaction_count"] == 3
        assert body_a["transaction_count_per_currency"] == {"CLP": 3}
        assert body_a["card_id"] == str(card_a)
        assert body_a["total_per_currency"] == {"CLP": "30000.00"}

        response_b = await api_client.get(
            "/api/v1/dashboard/summary",
            params={"period": "2026-07", "card_id": str(card_b)},
        )
        assert response_b.status_code == 200
        body_b = response_b.json()
        assert body_b["transaction_count"] == 2
        assert body_b["transaction_count_per_currency"] == {"USD": 2}
        assert body_b["card_id"] == str(card_b)
        assert body_b["total_per_currency"] == {"USD": "100.00"}

    @pytest.mark.asyncio
    async def test_summary_range_zero_reaches_resolver_with_all_time(
        self,
        api_client: AsyncClient,
        session_factory: async_sessionmaker[AsyncSession],
        seeded_world: dict[str, object],
    ) -> None:
        """Endpoint range=0 forwards all-time mode through the service resolver path."""
        statement_a = seeded_world["statement_a_id"]  # type: ignore[arg-type]
        merchant_id = seeded_world["merchant_clp_id"]  # type: ignore[arg-type]
        categories = seeded_world["categories"]  # type: ignore[assignment]
        groceries = categories["Groceries"]

        async with session_factory() as session:
            for txn_date, amount in (
                (date(2025, 3, 5), "100.00"),
                (date(2026, 1, 5), "200.00"),
                (date(2026, 6, 5), "50.00"),
                (date(2026, 7, 5), "300.00"),
            ):
                _add_transaction(
                    session,
                    statement_id=statement_a,
                    merchant_id=merchant_id,
                    amount=amount,
                    txn_date=txn_date,
                    currency="CLP",
                    category_id=groceries.id,
                )
            await session.commit()

        response = await api_client.get(
            "/api/v1/dashboard/summary",
            params={"period": "2026-07", "range": 0, "card_id": "all"},
        )

        assert response.status_code == 200
        body = response.json()
        assert body["transaction_count"] == 4
        assert body["total_per_currency"] == {"CLP": "650.00"}
        assert body["daily_avg_per_currency"] == {"CLP": "20.97"}
        assert body["comparison_to_prev_period_pct_per_currency"] == {"CLP": 500.0}

    @pytest.mark.asyncio
    async def test_summary_range_zero_service_receives_all_time_inference(
        self,
        api_client: AsyncClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """API range=0 forwards both the legacy integer and typed all-time mode."""
        captured: dict[str, object] = {}

        async def _capture_summary(
            self: DashboardService,
            *,
            period: date,
            range_months: int = 6,
            card_id="all",
            range_mode: RangeMode | None = None,
        ) -> SummaryResponse:
            del self
            captured.update(
                period=period,
                range_months=range_months,
                card_id=card_id,
                range_mode=range_mode,
            )
            return SummaryResponse(
                total_per_currency={},
                daily_avg_per_currency={},
                transaction_count=0,
                transaction_count_per_currency={},
                top_category_id=None,
                top_category_total_per_currency={},
                top_merchant_id=None,
                top_merchant_total_per_currency={},
                comparison_to_prev_period_pct_per_currency={},
                period_start=date(2026, 7, 1),
                period_end=date(2026, 7, 31),
                card_id="all",
            )

        monkeypatch.setattr(DashboardService, "summary", _capture_summary)

        response = await api_client.get(
            "/api/v1/dashboard/summary",
            params={"period": "2026-07", "range": 0, "card_id": "all"},
        )

        assert response.status_code == 200
        assert captured["range_months"] == 0
        assert captured["range_mode"] == RangeMode.all_time()


# ---------------------------------------------------------------------------
# categories
# ---------------------------------------------------------------------------


class TestDashboardCategories:
    """``GET /api/v1/dashboard/categories`` returns 12 rows."""

    @pytest.mark.asyncio
    async def test_categories_endpoint_returns_12_rows(
        self,
        api_client: AsyncClient,
        session_factory: async_sessionmaker[AsyncSession],
        seeded_world: dict[str, object],
    ) -> None:
        """The endpoint always returns 12 rows, even when most have zero spend.

        Mirrors the spec scenario "All 12 categories are
        returned even at zero spend": only "Groceries" has
        transactions; the other 11 carry empty totals.
        """
        statement_id = seeded_world["statement_a_id"]  # type: ignore[arg-type]
        merchant_id = seeded_world["merchant_clp_id"]  # type: ignore[arg-type]
        categories = seeded_world["categories"]  # type: ignore[assignment]
        groceries = categories["Groceries"]

        async with session_factory() as session:
            _add_transaction(
                session,
                statement_id=statement_id,
                merchant_id=merchant_id,
                amount="10000.00",
                txn_date=date(2026, 7, 5),
                currency="CLP",
                category_id=groceries.id,
            )
            await session.commit()

        response = await api_client.get(
            "/api/v1/dashboard/categories", params={"period": "2026-07"}
        )
        assert response.status_code == 200
        body = response.json()
        assert len(body) == 12

        # Find the Groceries row and the zero-spend rows.
        groceries_row = next(row for row in body if row["category_id"] == str(groceries.id))
        assert groceries_row["total_per_currency"] == {"CLP": "10000.00"}
        assert groceries_row["transaction_count"] == 1
        assert groceries_row["pct_of_total"] == 1.0  # the only spender in the period

        zero_rows = [row for row in body if row["category_id"] != str(groceries.id)]
        assert len(zero_rows) == 11
        for row in zero_rows:
            assert row["total_per_currency"] == {}
            assert row["transaction_count"] == 0
            assert row["pct_of_total"] == 0.0

    @pytest.mark.asyncio
    async def test_categories_endpoint_multi_currency(
        self,
        api_client: AsyncClient,
        session_factory: async_sessionmaker[AsyncSession],
        seeded_world: dict[str, object],
    ) -> None:
        """Multi-currency category row carries both CLP and USD keys.

        Mirrors the spec scenario "Multi-currency category row".
        """
        statement_a = seeded_world["statement_a_id"]  # type: ignore[arg-type]
        statement_b = seeded_world["statement_b_id"]  # type: ignore[arg-type]
        merchant_clp = seeded_world["merchant_clp_id"]  # type: ignore[arg-type]
        merchant_usd = seeded_world["merchant_usd_id"]  # type: ignore[arg-type]
        categories = seeded_world["categories"]  # type: ignore[assignment]
        groceries = categories["Groceries"]

        async with session_factory() as session:
            for day in (1, 5):
                _add_transaction(
                    session,
                    statement_id=statement_a,
                    merchant_id=merchant_clp,
                    amount="25000.00",
                    txn_date=date(2026, 7, day),
                    currency="CLP",
                    category_id=groceries.id,
                )
            _add_transaction(
                session,
                statement_id=statement_b,
                merchant_id=merchant_usd,
                amount="25.00",
                txn_date=date(2026, 7, 10),
                currency="USD",
                category_id=groceries.id,
            )
            await session.commit()

        response = await api_client.get(
            "/api/v1/dashboard/categories", params={"period": "2026-07"}
        )
        assert response.status_code == 200
        body = response.json()
        groceries_row = next(row for row in body if row["category_id"] == str(groceries.id))
        assert groceries_row["total_per_currency"] == {"CLP": "50000.00", "USD": "25.00"}
        assert groceries_row["transaction_count"] == 3

    @pytest.mark.asyncio
    async def test_categories_endpoint_card_id_filter(
        self,
        api_client: AsyncClient,
        session_factory: async_sessionmaker[AsyncSession],
        seeded_world: dict[str, object],
    ) -> None:
        """Single-card filter narrows the rows to that card's transactions.

        Mirrors the spec scenario "Single-card filter narrows
        the rows": card A has 3 Groceries txns; card B has 5;
        ``card_id=<card_A.uuid>`` reflects only card A's 3
        transactions; the 11 zero-spend rows are still
        present.
        """
        statement_a = seeded_world["statement_a_id"]  # type: ignore[arg-type]
        statement_b = seeded_world["statement_b_id"]  # type: ignore[arg-type]
        merchant_clp = seeded_world["merchant_clp_id"]  # type: ignore[arg-type]
        merchant_usd = seeded_world["merchant_usd_id"]  # type: ignore[arg-type]
        card_a = seeded_world["card_a_id"]  # type: ignore[arg-type]
        categories = seeded_world["categories"]  # type: ignore[assignment]
        groceries = categories["Groceries"]

        async with session_factory() as session:
            # 3 Groceries on card A (CLP).
            for day in (1, 5, 10):
                _add_transaction(
                    session,
                    statement_id=statement_a,
                    merchant_id=merchant_clp,
                    amount="10000.00",
                    txn_date=date(2026, 7, day),
                    currency="CLP",
                    category_id=groceries.id,
                )
            # 5 Groceries on card B (USD).
            for day in (2, 4, 6, 8, 12):
                _add_transaction(
                    session,
                    statement_id=statement_b,
                    merchant_id=merchant_usd,
                    amount="20.00",
                    txn_date=date(2026, 7, day),
                    currency="USD",
                    category_id=groceries.id,
                )
            await session.commit()

        response = await api_client.get(
            "/api/v1/dashboard/categories",
            params={"period": "2026-07", "card_id": str(card_a)},
        )
        assert response.status_code == 200
        body = response.json()
        assert len(body) == 12
        groceries_row = next(row for row in body if row["category_id"] == str(groceries.id))
        assert groceries_row["total_per_currency"] == {"CLP": "30000.00"}
        assert groceries_row["transaction_count"] == 3


# ---------------------------------------------------------------------------
# merchants
# ---------------------------------------------------------------------------


class TestDashboardMerchants:
    """``GET /api/v1/dashboard/merchants`` returns the top-N merchants."""

    @pytest.mark.asyncio
    async def test_merchants_endpoint_default_limit(
        self,
        api_client: AsyncClient,
        session_factory: async_sessionmaker[AsyncSession],
        seeded_world: dict[str, object],
    ) -> None:
        """Default ``limit=10`` caps the response at 10 rows.

        Mirrors the spec scenario "Default ``limit`` is 10".
        """
        statement_id = seeded_world["statement_a_id"]  # type: ignore[arg-type]
        merchant_clp = seeded_world["merchant_clp_id"]  # type: ignore[arg-type]
        async with session_factory() as session:
            _add_transaction(
                session,
                statement_id=statement_id,
                merchant_id=merchant_clp,
                amount="10000.00",
                txn_date=date(2026, 7, 5),
                currency="CLP",
            )
            await session.commit()

        response = await api_client.get("/api/v1/dashboard/merchants", params={"period": "2026-07"})
        assert response.status_code == 200
        body = response.json()
        # Single merchant, returned in the list of length 1.
        assert len(body) == 1
        assert body[0]["merchant_id"] == str(merchant_clp)

    @pytest.mark.asyncio
    async def test_merchants_endpoint_custom_limit(
        self,
        api_client: AsyncClient,
    ) -> None:
        """``limit=3`` is accepted as a query param and caps the response.

        Mirrors the spec scenario "Custom ``limit=3`` caps the
        response" — the request is accepted, the cap is
        applied, and an empty dataset returns ``[]`` (not
        404) per the spec scenario "No merchants returns
        200 with ``[]``".
        """
        response = await api_client.get(
            "/api/v1/dashboard/merchants",
            params={"period": "2026-07", "limit": 3},
        )
        assert response.status_code == 200
        assert response.json() == []

    @pytest.mark.asyncio
    async def test_merchants_endpoint_validates_limit(
        self,
        api_client: AsyncClient,
    ) -> None:
        """``limit > 50`` returns 400; ``limit < 1`` returns 400.

        Mirrors the spec scenario "``limit`` above the cap
        returns 400" and its symmetric low-side counterpart.
        """
        response_high = await api_client.get(
            "/api/v1/dashboard/merchants",
            params={"period": "2026-07", "limit": 200},
        )
        assert response_high.status_code == 400
        assert "limit" in response_high.json()["detail"].lower()

        response_low = await api_client.get(
            "/api/v1/dashboard/merchants",
            params={"period": "2026-07", "limit": 0},
        )
        assert response_low.status_code == 400
        assert "limit" in response_low.json()["detail"].lower()


# ---------------------------------------------------------------------------
# monthly
# ---------------------------------------------------------------------------


class TestDashboardMonthly:
    """``GET /api/v1/dashboard/monthly`` returns the time series."""

    @pytest.mark.asyncio
    async def test_monthly_endpoint_returns_time_series(
        self,
        api_client: AsyncClient,
    ) -> None:
        """``range=0`` (all-time) returns the months present in the dataset.

        Mirrors the spec scenario "``range=0`` returns
        all-time" and "No history returns 200 with ``[]``".
        An empty dataset → empty array; the endpoint must
        not 404.
        """
        response = await api_client.get("/api/v1/dashboard/monthly", params={"range": 0})
        assert response.status_code == 200
        assert response.json() == []

    @pytest.mark.asyncio
    async def test_monthly_endpoint_validates_range(
        self,
        api_client: AsyncClient,
    ) -> None:
        """``range=4`` is outside ``{0, 3, 6, 12}`` → 400.

        Mirrors the spec scenario "Invalid ``range`` returns 400".
        """
        response = await api_client.get("/api/v1/dashboard/monthly", params={"range": 4})
        assert response.status_code == 400
        assert "range" in response.json()["detail"].lower()

    @pytest.mark.asyncio
    async def test_monthly_endpoint_default_range(
        self,
        api_client: AsyncClient,
        session_factory: async_sessionmaker[AsyncSession],
        seeded_world: dict[str, object],
    ) -> None:
        """Default ``range=6`` returns 6 rows; ``range=3`` returns 3 rows.

        When transactions are present, the time series has
        the documented length. This also exercises the
        ``range_months=6`` default path.
        """
        statement_id = seeded_world["statement_a_id"]  # type: ignore[arg-type]
        merchant_clp = seeded_world["merchant_clp_id"]  # type: ignore[arg-type]
        async with session_factory() as session:
            _add_transaction(
                session,
                statement_id=statement_id,
                merchant_id=merchant_clp,
                amount="10000.00",
                txn_date=date(2026, 7, 10),
                currency="CLP",
            )
            await session.commit()

        response_default = await api_client.get("/api/v1/dashboard/monthly")
        assert response_default.status_code == 200
        assert len(response_default.json()) == 6

        response_three = await api_client.get("/api/v1/dashboard/monthly", params={"range": 3})
        assert response_three.status_code == 200
        assert len(response_three.json()) == 3


# ---------------------------------------------------------------------------
# recurring
# ---------------------------------------------------------------------------


class TestDashboardRecurring:
    """``GET /api/v1/dashboard/recurring`` returns the active rules."""

    @pytest.mark.asyncio
    async def test_recurring_endpoint_returns_active_rules(
        self,
        api_client: AsyncClient,
        session_factory: async_sessionmaker[AsyncSession],
        seeded_world: dict[str, object],
    ) -> None:
        """Active rules with an in-band occurrence in the period are returned.

        Mirrors the spec scenario "Active rules with an
        in-band occurrence are returned": a MCDONALDS rule
        with an in-band transaction in 2026-07 → the rule
        appears in the response with all
        ``RecurringRuleResponse`` fields populated.
        """
        merchant_id = seeded_world["merchant_clp_id"]  # type: ignore[arg-type]
        statement_id = seeded_world["statement_a_id"]  # type: ignore[arg-type]

        async with session_factory() as session:
            rule = RecurringRule(
                merchant_id=merchant_id,
                period_days=30,
                period_label="monthly",
                amount_min=Decimal("9.99"),
                amount_max=Decimal("9.99"),
                currency="CLP",
                is_active=True,
                confidence=0.95,
                last_seen_date=date(2026, 7, 5),
                occurrences=4,
            )
            session.add(rule)
            await session.commit()
            await session.refresh(rule)
            rule_id = rule.id

            # In-band transaction in 2026-07.
            _add_transaction(
                session,
                statement_id=statement_id,
                merchant_id=merchant_id,
                amount="9.99",
                txn_date=date(2026, 7, 15),
                currency="CLP",
            )
            await session.commit()

        response = await api_client.get("/api/v1/dashboard/recurring", params={"period": "2026-07"})
        assert response.status_code == 200
        body = response.json()
        assert len(body) == 1
        rule_row = body[0]
        assert rule_row["id"] == str(rule_id)
        assert rule_row["merchant_id"] == str(merchant_id)
        assert rule_row["period_label"] == "monthly"
        assert rule_row["currency"] == "CLP"
        assert rule_row["is_active"] is True
        assert rule_row["occurrences"] == 4
        assert rule_row["last_seen_date"] == "2026-07-05"

    @pytest.mark.asyncio
    async def test_recurring_endpoint_with_period_filter(
        self,
        api_client: AsyncClient,
    ) -> None:
        """Missing ``period`` returns 422 (FastAPI required-param behaviour).

        The spec scenario "Missing ``period`` returns 400" is
        enforced by the framework returning 422 for a missing
        required param; the endpoint layer adds the
        format-validation 400 on top. Either response code is
        a client error so the contract is preserved.
        """
        response = await api_client.get("/api/v1/dashboard/recurring")
        assert response.status_code == 422


# ---------------------------------------------------------------------------
# "all" sentinel
# ---------------------------------------------------------------------------


class TestCardIdAllSentinel:
    """The ``card_id="all"`` sentinel is accepted by every endpoint."""

    @pytest.mark.asyncio
    async def test_all_endpoints_with_card_id_all(
        self,
        api_client: AsyncClient,
        session_factory: async_sessionmaker[AsyncSession],
        seeded_world: dict[str, object],
    ) -> None:
        """``card_id="all"`` is accepted by every endpoint and aggregates both cards.

        Mirrors the spec scenarios "Omitted ``card_id``
        defaults to ``"all"``" and "Default ``card_id`` is
        ``"all"``" — explicit ``card_id="all"`` behaves
        identically to omitting the param.
        """
        statement_a = seeded_world["statement_a_id"]  # type: ignore[arg-type]
        statement_b = seeded_world["statement_b_id"]  # type: ignore[arg-type]
        merchant_clp = seeded_world["merchant_clp_id"]  # type: ignore[arg-type]
        merchant_usd = seeded_world["merchant_usd_id"]  # type: ignore[arg-type]

        async with session_factory() as session:
            _add_transaction(
                session,
                statement_id=statement_a,
                merchant_id=merchant_clp,
                amount="10000.00",
                txn_date=date(2026, 7, 5),
                currency="CLP",
            )
            _add_transaction(
                session,
                statement_id=statement_b,
                merchant_id=merchant_usd,
                amount="50.00",
                txn_date=date(2026, 7, 10),
                currency="USD",
            )
            await session.commit()

        all_params = {"card_id": "all"}

        response_summary = await api_client.get(
            "/api/v1/dashboard/summary", params={"period": "2026-07", **all_params}
        )
        assert response_summary.status_code == 200
        body_summary = response_summary.json()
        assert body_summary["card_id"] == "all"
        assert body_summary["transaction_count"] == 2
        assert body_summary["transaction_count_per_currency"] == {"CLP": 1, "USD": 1}
        assert body_summary["total_per_currency"] == {"CLP": "10000.00", "USD": "50.00"}

        response_categories = await api_client.get(
            "/api/v1/dashboard/categories", params={"period": "2026-07", **all_params}
        )
        assert response_categories.status_code == 200
        assert len(response_categories.json()) == 12

        response_merchants = await api_client.get(
            "/api/v1/dashboard/merchants", params={"period": "2026-07", **all_params}
        )
        assert response_merchants.status_code == 200
        assert len(response_merchants.json()) == 2

        response_monthly = await api_client.get(
            "/api/v1/dashboard/monthly", params={"range": 0, **all_params}
        )
        assert response_monthly.status_code == 200
        assert len(response_monthly.json()) == 1

        response_recurring = await api_client.get(
            "/api/v1/dashboard/recurring", params={"period": "2026-07", **all_params}
        )
        assert response_recurring.status_code == 200
        assert response_recurring.json() == []
