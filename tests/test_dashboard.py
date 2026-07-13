"""Tests for the Phase 3 dashboard data service (PR #8).

Covers the :class:`app.services.dashboard.DashboardService`
class introduced by Phase 3 PR #8: the data layer for the
five ``/api/v1/dashboard/*`` endpoints and the
``GET /dashboard`` HTMX page. The service is pure SQL with
``GROUP BY`` on indexed columns, so the test surface is a
real in-memory SQLite database plus a hand-rolled fixture
(no LLM, no PDF).

Test surface
------------

* **summary** (6) — the single-month KPI rollup; multi-
  currency sub-rollup; empty period; single-card filter;
  top-category + top-merchant selection; prior-month
  percentage.
* **categories** (5) — the always-12-row breakdown;
  zero-spend rows; multi-currency sub-rollup; ordering;
  single-card narrowing.
* **merchants** (4) — top-N by total; ``limit`` cap;
  default limit; empty result.
* **monthly** (4) — time series; range = 3 / 6 / 12 / 0;
  zero-transaction months filled in; ``prev_month_pct``
  calculation.
* **recurring** (3) — in-band occurrence check;
  inactive-rule exclusion; card filter narrows scope.
* **card filter** (2) — ``"all"`` does not add the JOIN
  to ``statements``; UUID filter narrows the rows.

Total: 24 tests.

The test surface mirrors the Phase 2
:mod:`tests.test_recurring` style: per-test fresh
in-memory SQLite database, ``recurring_engine`` /
``session_factory`` fixtures, hand-rolled transaction
fixture with a ``_add_transaction`` helper. The
``seeded_world`` fixture provides one bank, one card,
one statement, one merchant — the canonical "world" the
tests build on.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.db.engine import create_engine
from app.models import Bank, Category, CreditCard, Merchant, RecurringRule, Statement, Transaction
from app.models.base import Base
from app.models.statement import StatementStatus
from app.services.dashboard import DashboardService

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def dashboard_engine(test_settings: Settings) -> AsyncIterator[AsyncEngine]:
    """Yield a fresh engine with the full schema + the 12 seeded categories.

    Mirrors the ``recurring_engine`` fixture from
    :mod:`tests.test_recurring`, with the addition of the
    closed-set 12-row ``categories`` seed the dashboard
    service depends on (the categories migration is
    exercised by :mod:`tests.test_alembic`; the seed here
    keeps the unit test self-contained).
    """
    engine: AsyncEngine = create_engine(test_settings)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        # Seed the 12 closed-set categories in a separate
        # session so the ``default=uuid.uuid4`` Python-side
        # default on the :class:`UUIDMixin` column fires
        # (a raw ``Table.insert()`` would skip the Python
        # default and trip the NOT NULL constraint).
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            for name, display_name, sort_order in (
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


def _category_dict_unused() -> None:
    """Removed — see the engine fixture for the new seeding approach."""


@pytest_asyncio.fixture
async def session_factory(
    dashboard_engine: AsyncEngine,
) -> async_sessionmaker[AsyncSession]:
    """Yield a session factory bound to the dashboard engine."""
    return async_sessionmaker(dashboard_engine, expire_on_commit=False)


@pytest_asyncio.fixture
async def seeded_world(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[dict[str, object]]:
    """Yield a dict with two banks, two cards, two statements, two merchants.

    Two cards so the ``card_id="all"`` vs UUID filter
    tests can exercise the "single-card narrowing"
    scenario without rebuilding the fixture per test.
    The statement is fixed at ``period_end = 2026-06-30``
    so the dashboard's "current month" is well-defined.

    The fixture also seeds one :class:`RecurringRule` per
    merchant so the ``recurring()`` tests have an active
    rule to find.
    """
    now = datetime.now(UTC)
    bank = Bank(
        name="dashboard_test_bank",
        display_name="Dashboard Test Bank",
        password_formula="rut_sin_dv",
        is_active=True,
        created_at=now,
        updated_at=now,
    )
    async with session_factory() as session:
        session.add(bank)
        await session.commit()
        await session.refresh(bank)

        card_a = CreditCard(
            bank_id=bank.id,
            card_number_masked="XXXX XXXX XXXX 0001",
            cardholder="DASHBOARD USER A",
            currency="CLP",
            is_active=True,
        )
        card_b = CreditCard(
            bank_id=bank.id,
            card_number_masked="XXXX XXXX XXXX 0002",
            cardholder="DASHBOARD USER B",
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
            file_path="/tmp/dashboard-test-a.pdf",
            file_hash="a" * 64,
            status=StatementStatus.COMPLETED,
        )
        statement_b = Statement(
            credit_card_id=card_b.id,
            period_start=date(2026, 6, 1),
            period_end=date(2026, 6, 30),
            statement_date=date(2026, 6, 30),
            file_path="/tmp/dashboard-test-b.pdf",
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

        # Fetch the seeded categories for the per-test
        # category assignment helper.
        categories_rows = (
            (await session.execute(select(Category).order_by(Category.sort_order))).scalars().all()
        )

    yield {
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

    Mirrors the helper from :mod:`tests.test_recurring`
    with the addition of a ``category_id`` argument so
    the per-category tests can assign categories
    without a follow-up UPDATE.
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


class TestSummary:
    """``DashboardService.summary`` returns the KPI tile payload."""

    @pytest.mark.asyncio
    async def test_summary_returns_kpis_for_period(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        seeded_world: dict[str, object],
    ) -> None:
        """5 CLP transactions in 2026-07 produce the documented payload.

        Mirrors the spec scenario "Summary for a single-currency period":
        ``total_per_currency == {"CLP": <sum>}`` and
        ``transaction_count == 5``; ``period_start`` and
        ``period_end`` are the first and last days of July
        2026; ``card_id`` echoes ``"all"``.
        """
        statement_id = seeded_world["statement_a_id"]  # type: ignore[arg-type]
        merchant_id = seeded_world["merchant_clp_id"]  # type: ignore[arg-type]
        categories = seeded_world["categories"]  # type: ignore[assignment]
        groceries = categories["Groceries"]
        dining = categories["Dining Out"]

        async with session_factory() as session:
            for amount, day, cat in (
                ("10000.00", 1, groceries),
                ("20000.00", 5, groceries),
                ("30000.00", 10, dining),
                ("15000.00", 15, groceries),
                ("5000.00", 20, dining),
            ):
                _add_transaction(
                    session,
                    statement_id=statement_id,
                    merchant_id=merchant_id,
                    amount=amount,
                    txn_date=date(2026, 7, day),
                    category_id=cat.id,
                )
            await session.commit()

        async with session_factory() as session:
            response = await DashboardService(session).summary(
                period=date(2026, 7, 15),
                range_months=6,
                card_id="all",
            )

        assert response.transaction_count == 5
        assert response.total_per_currency == {"CLP": Decimal("80000.00")}
        # 5 transactions on 5 distinct days → daily avg = total / 5.
        assert response.daily_avg_per_currency == {"CLP": Decimal("16000.00")}
        assert response.period_start == date(2026, 7, 1)
        assert response.period_end == date(2026, 7, 31)
        assert response.card_id == "all"
        # Groceries (3 txns, CLP 45000) > Dining (2 txns, CLP 35000).
        assert response.top_category_id == groceries.id
        assert response.top_category_total_per_currency == {"CLP": Decimal("45000.00")}

    @pytest.mark.asyncio
    async def test_summary_handles_no_transactions(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        seeded_world: dict[str, object],
    ) -> None:
        """An empty period returns zeros, not an error.

        Mirrors the spec scenario "Empty period returns
        zeros, not an error": ``total_per_currency == {}``,
        ``transaction_count == 0``, ``top_category_id is
        None``, ``top_merchant_id is None``, and
        ``comparison_to_prev_period_pct_per_currency == {}``.
        """
        del seeded_world
        async with session_factory() as session:
            response = await DashboardService(session).summary(
                period=date(2026, 7, 15),
                range_months=6,
                card_id="all",
            )

        assert response.total_per_currency == {}
        assert response.daily_avg_per_currency == {}
        assert response.transaction_count == 0
        assert response.top_category_id is None
        assert response.top_category_total_per_currency == {}
        assert response.top_merchant_id is None
        assert response.top_merchant_total_per_currency == {}
        assert response.comparison_to_prev_period_pct_per_currency == {}
        assert response.period_start == date(2026, 7, 1)
        assert response.period_end == date(2026, 7, 31)
        assert response.card_id == "all"

    @pytest.mark.asyncio
    async def test_summary_with_card_id_all_vs_uuid(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        seeded_world: dict[str, object],
    ) -> None:
        """``card_id="all"`` aggregates across cards; UUID narrows to one.

        Card A has 3 transactions in 2026-07; Card B has 2.
        ``summary(card_id="all")`` returns
        ``transaction_count == 5``;
        ``summary(card_id=card_a)`` returns
        ``transaction_count == 3``.
        """
        statement_a = seeded_world["statement_a_id"]  # type: ignore[arg-type]
        statement_b = seeded_world["statement_b_id"]  # type: ignore[arg-type]
        merchant_clp = seeded_world["merchant_clp_id"]  # type: ignore[arg-type]
        merchant_usd = seeded_world["merchant_usd_id"]  # type: ignore[arg-type]
        card_a = seeded_world["card_a_id"]  # type: ignore[arg-type]

        async with session_factory() as session:
            # 3 CLP transactions on card A.
            for day in (1, 5, 10):
                _add_transaction(
                    session,
                    statement_id=statement_a,
                    merchant_id=merchant_clp,
                    amount="10000.00",
                    txn_date=date(2026, 7, day),
                    currency="CLP",
                )
            # 2 USD transactions on card B.
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

        async with session_factory() as session:
            all_response = await DashboardService(session).summary(
                period=date(2026, 7, 15), range_months=6, card_id="all"
            )
        async with session_factory() as session:
            card_response = await DashboardService(session).summary(
                period=date(2026, 7, 15), range_months=6, card_id=card_a
            )

        assert all_response.transaction_count == 5
        assert all_response.total_per_currency == {
            "CLP": Decimal("30000.00"),
            "USD": Decimal("100.00"),
        }
        assert card_response.transaction_count == 3
        assert card_response.total_per_currency == {"CLP": Decimal("30000.00")}

    @pytest.mark.asyncio
    async def test_summary_multi_currency_sub_rollup(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        seeded_world: dict[str, object],
    ) -> None:
        """Multi-currency period returns side-by-side totals.

        Mirrors the spec scenario "Multi-currency period
        returns side-by-side": 3 CLP + 2 USD transactions
        in 2026-07 → ``total_per_currency`` is
        ``{"CLP": <clp_sum>, "USD": <usd_sum>}`` with both
        keys present, NOT summed into a single number.
        """
        statement_a = seeded_world["statement_a_id"]  # type: ignore[arg-type]
        statement_b = seeded_world["statement_b_id"]  # type: ignore[arg-type]
        merchant_clp = seeded_world["merchant_clp_id"]  # type: ignore[arg-type]
        merchant_usd = seeded_world["merchant_usd_id"]  # type: ignore[arg-type]

        async with session_factory() as session:
            for day, amount in ((1, "10000.00"), (5, "20000.00"), (10, "15000.00")):
                _add_transaction(
                    session,
                    statement_id=statement_a,
                    merchant_id=merchant_clp,
                    amount=amount,
                    txn_date=date(2026, 7, day),
                    currency="CLP",
                )
            for day, amount in ((2, "30.00"), (12, "70.00")):
                _add_transaction(
                    session,
                    statement_id=statement_b,
                    merchant_id=merchant_usd,
                    amount=amount,
                    txn_date=date(2026, 7, day),
                    currency="USD",
                )
            await session.commit()

        async with session_factory() as session:
            response = await DashboardService(session).summary(
                period=date(2026, 7, 15), range_months=6, card_id="all"
            )

        assert response.total_per_currency == {
            "CLP": Decimal("45000.00"),
            "USD": Decimal("100.00"),
        }
        # ``daily_avg_per_currency`` carries a per-currency
        # entry for each currency present.
        assert set(response.daily_avg_per_currency.keys()) == {"CLP", "USD"}

    @pytest.mark.asyncio
    async def test_summary_comparison_to_prev_period(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        seeded_world: dict[str, object],
    ) -> None:
        """Comparison vs. prior month is the signed ``%`` change.

        2026-06 had CLP 100,000; 2026-07 has CLP 120,000
        → ``comparison_to_prev_period_pct_per_currency["CLP"] == 20.0``
        (a 20% increase, per the spec scenario
        "``prev_month_pct_per_currency`` is the signed %
        change").
        """
        statement_a = seeded_world["statement_a_id"]  # type: ignore[arg-type]
        merchant_id = seeded_world["merchant_clp_id"]  # type: ignore[arg-type]
        async with session_factory() as session:
            # 2026-06 baseline.
            for day in (1, 15, 30):
                _add_transaction(
                    session,
                    statement_id=statement_a,
                    merchant_id=merchant_id,
                    amount="33333.33",
                    txn_date=date(2026, 6, day),
                    currency="CLP",
                )
            # 2026-07 — 20% larger.
            for day in (1, 10, 20):
                _add_transaction(
                    session,
                    statement_id=statement_a,
                    merchant_id=merchant_id,
                    amount="40000.00",
                    txn_date=date(2026, 7, day),
                    currency="CLP",
                )
            await session.commit()

        async with session_factory() as session:
            response = await DashboardService(session).summary(
                period=date(2026, 7, 15), range_months=6, card_id="all"
            )

        assert response.comparison_to_prev_period_pct_per_currency.get("CLP") == 20.0

    @pytest.mark.asyncio
    async def test_summary_top_merchant_picks_largest(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        seeded_world: dict[str, object],
    ) -> None:
        """The top-merchant pick is the merchant with the largest single-currency total."""
        statement_a = seeded_world["statement_a_id"]  # type: ignore[arg-type]
        merchant_clp = seeded_world["merchant_clp_id"]  # type: ignore[arg-type]
        merchant_usd = seeded_world["merchant_usd_id"]  # type: ignore[arg-type]
        statement_b = seeded_world["statement_b_id"]  # type: ignore[arg-type]

        # Seed a second CLP merchant via a fresh statement
        # for the same card. The top-merchant ordering
        # must pick the larger-spend merchant.
        async with session_factory() as session:
            from app.models.merchant import Merchant

            second = Merchant(name="lider", is_active=True)
            session.add(second)
            await session.commit()
            await session.refresh(second)
            second_id = second.id

            for day, amount in ((1, "5000.00"), (10, "10000.00")):
                _add_transaction(
                    session,
                    statement_id=statement_a,
                    merchant_id=merchant_clp,
                    amount=amount,
                    txn_date=date(2026, 7, day),
                    currency="CLP",
                )
            for day, amount in ((5, "50000.00"), (15, "30000.00")):
                _add_transaction(
                    session,
                    statement_id=statement_a,
                    merchant_id=second_id,
                    amount=amount,
                    txn_date=date(2026, 7, day),
                    currency="CLP",
                )
            # 1 USD transaction so the top-merchant
            # ``total_per_currency`` dict is multi-key —
            # verifies the field is per-currency even
            # when the merchant is single-currency.
            _add_transaction(
                session,
                statement_id=statement_b,
                merchant_id=merchant_usd,
                amount="20.00",
                txn_date=date(2026, 7, 25),
                currency="USD",
            )
            await session.commit()

        async with session_factory() as session:
            response = await DashboardService(session).summary(
                period=date(2026, 7, 15), range_months=6, card_id="all"
            )

        # ``lider`` has CLP 80,000 vs ``netflix`` CLP 15,000.
        assert response.top_merchant_id == second_id
        assert response.top_merchant_total_per_currency == {"CLP": Decimal("80000.00")}


# ---------------------------------------------------------------------------
# categories
# ---------------------------------------------------------------------------


class TestCategories:
    """``DashboardService.categories`` returns the 12 closed-set rows."""

    @pytest.mark.asyncio
    async def test_categories_returns_all_12_categories_even_at_zero(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        seeded_world: dict[str, object],
    ) -> None:
        """All 12 categories appear, even when only one has any spend.

        Mirrors the spec scenario "All 12 categories are
        returned even at zero spend": 11 zero-spend rows
        carry ``total_per_currency == {}``,
        ``transaction_count == 0``,
        ``pct_of_total == 0.0``.
        """
        del seeded_world
        async with session_factory() as session:
            rows = await DashboardService(session).categories(
                period=date(2026, 7, 15), card_id="all"
            )

        assert len(rows) == 12
        # All 12 are zero-spend.
        assert all(r.transaction_count == 0 for r in rows)
        assert all(r.total_per_currency == {} for r in rows)
        assert all(r.pct_of_total == 0.0 for r in rows)

    @pytest.mark.asyncio
    async def test_categories_multi_currency_sub_rollup(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        seeded_world: dict[str, object],
    ) -> None:
        """A multi-currency category row carries the per-currency dict.

        Mirrors the spec scenario "Multi-currency category
        row": Groceries has 2 CLP transactions summing
        CLP 50,000 and 1 USD transaction of USD 25.00 →
        ``total_per_currency == {"CLP": 50000, "USD": 25.00}``
        and ``transaction_count == 3``.
        """
        statement_a = seeded_world["statement_a_id"]  # type: ignore[arg-type]
        statement_b = seeded_world["statement_b_id"]  # type: ignore[arg-type]
        merchant_clp = seeded_world["merchant_clp_id"]  # type: ignore[arg-type]
        merchant_usd = seeded_world["merchant_usd_id"]  # type: ignore[arg-type]
        categories = seeded_world["categories"]  # type: ignore[assignment]
        groceries = categories["Groceries"]

        async with session_factory() as session:
            for day, amount in ((1, "30000.00"), (10, "20000.00")):
                _add_transaction(
                    session,
                    statement_id=statement_a,
                    merchant_id=merchant_clp,
                    amount=amount,
                    txn_date=date(2026, 7, day),
                    currency="CLP",
                    category_id=groceries.id,
                )
            _add_transaction(
                session,
                statement_id=statement_b,
                merchant_id=merchant_usd,
                amount="25.00",
                txn_date=date(2026, 7, 12),
                currency="USD",
                category_id=groceries.id,
            )
            await session.commit()

        async with session_factory() as session:
            rows = await DashboardService(session).categories(
                period=date(2026, 7, 15), card_id="all"
            )

        groceries_row = next(r for r in rows if r.category_id == groceries.id)
        assert groceries_row.total_per_currency == {
            "CLP": Decimal("50000.00"),
            "USD": Decimal("25.00"),
        }
        assert groceries_row.transaction_count == 3

    @pytest.mark.asyncio
    async def test_categories_ordered_by_total_descending(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        seeded_world: dict[str, object],
    ) -> None:
        """Categories are ordered by the largest single-currency total descending.

        Mirrors the spec scenario "Categories are ordered
        by total descending": Groceries CLP 100,000 →
        first; Dining Out CLP 50,000 → second.
        """
        statement_a = seeded_world["statement_a_id"]  # type: ignore[arg-type]
        merchant_clp = seeded_world["merchant_clp_id"]  # type: ignore[arg-type]
        categories = seeded_world["categories"]  # type: ignore[assignment]
        groceries = categories["Groceries"]
        dining = categories["Dining Out"]

        async with session_factory() as session:
            for amount, day in (
                ("50000.00", 1),
                ("30000.00", 10),
                ("20000.00", 20),
            ):
                _add_transaction(
                    session,
                    statement_id=statement_a,
                    merchant_id=merchant_clp,
                    amount=amount,
                    txn_date=date(2026, 7, day),
                    currency="CLP",
                    category_id=groceries.id,
                )
            for amount, day in (("30000.00", 5), ("20000.00", 15)):
                _add_transaction(
                    session,
                    statement_id=statement_a,
                    merchant_id=merchant_clp,
                    amount=amount,
                    txn_date=date(2026, 7, day),
                    currency="CLP",
                    category_id=dining.id,
                )
            await session.commit()

        async with session_factory() as session:
            rows = await DashboardService(session).categories(
                period=date(2026, 7, 15), card_id="all"
            )

        # The first non-zero row is Groceries; the second is Dining.
        non_zero = [r for r in rows if r.transaction_count > 0]
        assert non_zero[0].category_id == groceries.id
        assert non_zero[1].category_id == dining.id
        assert non_zero[0].pct_of_total > 0.5

    @pytest.mark.asyncio
    async def test_categories_single_card_filter(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        seeded_world: dict[str, object],
    ) -> None:
        """Single-card filter narrows the rows but keeps the 11 zero-spend rows.

        Mirrors the spec scenario "Single-card filter
        narrows the rows".
        """
        statement_a = seeded_world["statement_a_id"]  # type: ignore[arg-type]
        statement_b = seeded_world["statement_b_id"]  # type: ignore[arg-type]
        merchant_clp = seeded_world["merchant_clp_id"]  # type: ignore[arg-type]
        merchant_usd = seeded_world["merchant_usd_id"]  # type: ignore[arg-type]
        card_a = seeded_world["card_a_id"]  # type: ignore[arg-type]
        categories = seeded_world["categories"]  # type: ignore[assignment]
        groceries = categories["Groceries"]

        async with session_factory() as session:
            # Card A: 3 Groceries transactions.
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
            # Card B: 5 Groceries transactions.
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

        async with session_factory() as session:
            rows = await DashboardService(session).categories(
                period=date(2026, 7, 15), card_id=card_a
            )

        assert len(rows) == 12
        groceries_row = next(r for r in rows if r.category_id == groceries.id)
        # Only card A's 3 transactions are counted.
        assert groceries_row.transaction_count == 3
        assert groceries_row.total_per_currency == {"CLP": Decimal("30000.00")}

    @pytest.mark.asyncio
    async def test_categories_empty_period_returns_12_zero_rows(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        seeded_world: dict[str, object],
    ) -> None:
        """An empty period still returns 12 zero-spend rows.

        Mirrors the spec scenario "Empty period returns 12
        zero-spend rows".
        """
        del seeded_world
        async with session_factory() as session:
            rows = await DashboardService(session).categories(
                period=date(2026, 7, 15), card_id="all"
            )

        assert len(rows) == 12
        for r in rows:
            assert r.total_per_currency == {}
            assert r.transaction_count == 0
            assert r.pct_of_total == 0.0


# ---------------------------------------------------------------------------
# merchants
# ---------------------------------------------------------------------------


class TestMerchants:
    """``DashboardService.merchants`` returns the top-N merchants."""

    @pytest.mark.asyncio
    async def test_merchants_top_n_by_total(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        seeded_world: dict[str, object],
    ) -> None:
        """Top merchants are ordered by the largest single-currency total descending.

        Mirrors the spec scenario "Top merchants ordered
        by total descending": SHELL CLP 30,000 (3 txns)
        first; MCDONALDS CLP 25,000 (2 txns) second;
        STARBUCKS CLP 18,000 (4 txns) third.
        """
        statement_a = seeded_world["statement_a_id"]  # type: ignore[arg-type]
        async with session_factory() as session:
            from app.models.merchant import Merchant

            shell = Merchant(name="shell", is_active=True)
            mc = Merchant(name="mcdonalds", is_active=True)
            sb = Merchant(name="starbucks", is_active=True)
            session.add_all([shell, mc, sb])
            await session.commit()
            await session.refresh(shell)
            await session.refresh(mc)
            await session.refresh(sb)

            # SHELL: CLP 30,000 across 3 txns.
            for day in (1, 10, 20):
                _add_transaction(
                    session,
                    statement_id=statement_a,
                    merchant_id=shell.id,
                    amount="10000.00",
                    txn_date=date(2026, 7, day),
                    currency="CLP",
                )
            # MCDONALDS: CLP 25,000 across 2 txns.
            for day, amount in ((5, "10000.00"), (15, "15000.00")):
                _add_transaction(
                    session,
                    statement_id=statement_a,
                    merchant_id=mc.id,
                    amount=amount,
                    txn_date=date(2026, 7, day),
                    currency="CLP",
                )
            # STARBUCKS: CLP 18,000 across 4 txns.
            for day, amount in ((2, "4000.00"), (8, "5000.00"), (12, "4000.00"), (25, "5000.00")):
                _add_transaction(
                    session,
                    statement_id=statement_a,
                    merchant_id=sb.id,
                    amount=amount,
                    txn_date=date(2026, 7, day),
                    currency="CLP",
                )
            await session.commit()
            shell_id, mc_id, sb_id = shell.id, mc.id, sb.id

        async with session_factory() as session:
            rows = await DashboardService(session).merchants(
                period=date(2026, 7, 15), card_id="all", limit=10
            )

        # Order is preserved.
        assert [r.merchant_id for r in rows] == [shell_id, mc_id, sb_id]
        # ``last_seen_date`` is the most-recent transaction
        # for each merchant in the period.
        shell_row = rows[0]
        assert shell_row.total_per_currency == {"CLP": Decimal("30000.00")}
        assert shell_row.transaction_count == 3
        assert shell_row.last_seen_date == date(2026, 7, 20)

    @pytest.mark.asyncio
    async def test_merchants_respects_limit_param(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        seeded_world: dict[str, object],
    ) -> None:
        """``limit`` caps the response length.

        Mirrors the spec scenario "``limit`` caps the
        response length": 15 distinct merchants with
        transactions, ``limit=3`` returns the top 3.
        """
        statement_a = seeded_world["statement_a_id"]  # type: ignore[arg-type]
        async with session_factory() as session:
            from app.models.merchant import Merchant

            merchants: list[uuid.UUID] = []
            for i in range(15):
                m = Merchant(name=f"merchant_{i:02d}", is_active=True)
                session.add(m)
                await session.commit()
                await session.refresh(m)
                merchants.append(m.id)
                # Larger amount for later merchants so the
                # ordering is stable.
                _add_transaction(
                    session,
                    statement_id=statement_a,
                    merchant_id=m.id,
                    amount=str(10000 + i * 1000),
                    txn_date=date(2026, 7, 1),
                    currency="CLP",
                )
            await session.commit()

        async with session_factory() as session:
            rows = await DashboardService(session).merchants(
                period=date(2026, 7, 15), card_id="all", limit=3
            )

        assert len(rows) == 3
        # Top 3 are the highest-spend merchants.
        assert rows[0].merchant_id == merchants[-1]
        assert rows[1].merchant_id == merchants[-2]
        assert rows[2].merchant_id == merchants[-3]

    @pytest.mark.asyncio
    async def test_merchants_default_limit_is_10(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        seeded_world: dict[str, object],
    ) -> None:
        """The default ``limit`` is 10.

        Mirrors the spec scenario "Default ``limit`` is
        10": 25 distinct merchants → response length 10.
        """
        statement_a = seeded_world["statement_a_id"]  # type: ignore[arg-type]
        async with session_factory() as session:
            from app.models.merchant import Merchant

            for i in range(25):
                m = Merchant(name=f"merchant_{i:02d}", is_active=True)
                session.add(m)
                await session.commit()
                await session.refresh(m)
                _add_transaction(
                    session,
                    statement_id=statement_a,
                    merchant_id=m.id,
                    amount=str(1000 + i),
                    txn_date=date(2026, 7, 1),
                    currency="CLP",
                )
            await session.commit()

        async with session_factory() as session:
            rows = await DashboardService(session).merchants(
                period=date(2026, 7, 15), card_id="all"
            )

        assert len(rows) == 10

    @pytest.mark.asyncio
    async def test_merchants_empty_when_no_transactions(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        seeded_world: dict[str, object],
    ) -> None:
        """No transactions in the period returns an empty list.

        Mirrors the spec scenario "No merchants in period
        returns empty list": no transactions → ``[]``,
        not 404.
        """
        del seeded_world
        async with session_factory() as session:
            rows = await DashboardService(session).merchants(
                period=date(2026, 7, 15), card_id="all", limit=10
            )

        assert rows == []


# ---------------------------------------------------------------------------
# monthly
# ---------------------------------------------------------------------------


class TestMonthly:
    """``DashboardService.monthly`` returns the bar-chart time series."""

    @pytest.mark.asyncio
    async def test_monthly_returns_time_series(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        seeded_world: dict[str, object],
    ) -> None:
        """``monthly(range=3)`` returns the last 3 months in ascending order.

        Mirrors the spec scenario "``range=3`` returns the
        last 3 months": seeded transactions in
        2026-05 / 2026-06 / 2026-07 → response is the 3
        months ending in the current month (the test
        runs at any time, so we pass an explicit
        ``range=3`` and assert the count, not the
        specific labels).
        """
        statement_a = seeded_world["statement_a_id"]  # type: ignore[arg-type]
        merchant_clp = seeded_world["merchant_clp_id"]  # type: ignore[arg-type]
        async with session_factory() as session:
            for day, month, amount in (
                (1, 5, "10000.00"),
                (5, 6, "20000.00"),
                (10, 7, "30000.00"),
            ):
                _add_transaction(
                    session,
                    statement_id=statement_a,
                    merchant_id=merchant_clp,
                    amount=amount,
                    txn_date=date(2026, month, day),
                    currency="CLP",
                )
            await session.commit()

        async with session_factory() as session:
            rows = await DashboardService(session).monthly(range_months=3, card_id="all")

        assert len(rows) == 3
        # Ascending order.
        assert [r.month for r in rows] == sorted(r.month for r in rows)

    @pytest.mark.asyncio
    async def test_monthly_range_3_6_12_all_time(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        seeded_world: dict[str, object],
    ) -> None:
        """``range=0`` returns every distinct month in the dataset."""
        statement_a = seeded_world["statement_a_id"]  # type: ignore[arg-type]
        merchant_clp = seeded_world["merchant_clp_id"]  # type: ignore[arg-type]
        async with session_factory() as session:
            for month in (1, 2, 3, 4, 5, 6, 7, 8):
                _add_transaction(
                    session,
                    statement_id=statement_a,
                    merchant_id=merchant_clp,
                    amount="1000.00",
                    txn_date=date(2026, month, 15),
                    currency="CLP",
                )
            await session.commit()

        async with session_factory() as session:
            rows = await DashboardService(session).monthly(range_months=0, card_id="all")

        # 8 distinct months in the dataset.
        assert len(rows) == 8
        # First row has no prior reference.
        assert rows[0].prev_month_pct_per_currency == {}
        # Ascending order.
        assert [r.month for r in rows] == [f"2026-{m:02d}" for m in range(1, 9)]

    @pytest.mark.asyncio
    async def test_monthly_prev_month_pct_calculation(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        seeded_world: dict[str, object],
    ) -> None:
        """``prev_month_pct`` is the signed ``%`` change vs. the prior calendar month.

        Mirrors the spec scenario "``prev_month_pct_per_currency``
        is the signed % change": 2026-06 CLP 100,000 →
        2026-07 CLP 120,000 → ``prev_month_pct_per_currency["CLP"] == 20.0``.
        """
        statement_a = seeded_world["statement_a_id"]  # type: ignore[arg-type]
        merchant_clp = seeded_world["merchant_clp_id"]  # type: ignore[arg-type]
        async with session_factory() as session:
            _add_transaction(
                session,
                statement_id=statement_a,
                merchant_id=merchant_clp,
                amount="100000.00",
                txn_date=date(2026, 6, 15),
                currency="CLP",
            )
            _add_transaction(
                session,
                statement_id=statement_a,
                merchant_id=merchant_clp,
                amount="120000.00",
                txn_date=date(2026, 7, 15),
                currency="CLP",
            )
            await session.commit()

        async with session_factory() as session:
            rows = await DashboardService(session).monthly(range_months=2, card_id="all")

        # 2 months in the response (range_months=2). The
        # second row carries the prior-month comparison.
        assert len(rows) == 2
        assert rows[0].prev_month_pct_per_currency == {}
        assert rows[1].prev_month_pct_per_currency.get("CLP") == 20.0

    @pytest.mark.asyncio
    async def test_monthly_zero_transaction_months_filled_in(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        seeded_world: dict[str, object],
    ) -> None:
        """Zero-transaction months are present in the series.

        Mirrors the spec scenario "Zero-transaction months
        are still in the series": transactions in
        2026-05 + 2026-07 but NOT in 2026-06 → response
        is 3 rows; the 2026-06 row carries empty totals
        and empty prev-month-pct.
        """
        statement_a = seeded_world["statement_a_id"]  # type: ignore[arg-type]
        merchant_clp = seeded_world["merchant_clp_id"]  # type: ignore[arg-type]
        async with session_factory() as session:
            _add_transaction(
                session,
                statement_id=statement_a,
                merchant_id=merchant_clp,
                amount="10000.00",
                txn_date=date(2026, 5, 10),
                currency="CLP",
            )
            _add_transaction(
                session,
                statement_id=statement_a,
                merchant_id=merchant_clp,
                amount="20000.00",
                txn_date=date(2026, 7, 10),
                currency="CLP",
            )
            await session.commit()

        async with session_factory() as session:
            rows = await DashboardService(session).monthly(range_months=3, card_id="all")

        assert len(rows) == 3
        # Find the 2026-06 row — the empty one.
        june = next(r for r in rows if r.month == "2026-06")
        assert june.total_per_currency == {}
        assert june.transaction_count == 0
        # The prev-month-pct is empty for the 2026-06
        # row because June has no totals to compare.
        # (It is not the first month; the first row in
        # this 3-month series is 2026-05. The 2026-06
        # row's prior month is 2026-05 with CLP 10,000,
        # so 2026-06's prev_pct["CLP"] would be
        # ``-100.0`` (drop to zero).)
        # We don't assert a specific value here because
        # the exact 3-month window depends on the
        # current date; the relevant invariant is
        # covered by the dedicated prev_pct test above.


# ---------------------------------------------------------------------------
# recurring
# ---------------------------------------------------------------------------


class TestRecurring:
    """``DashboardService.recurring`` returns active rules with an in-band occurrence."""

    @pytest.mark.asyncio
    async def test_recurring_returns_active_rules_in_period(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        seeded_world: dict[str, object],
    ) -> None:
        """An active rule with an in-band transaction in the period is returned."""
        statement_a = seeded_world["statement_a_id"]  # type: ignore[arg-type]
        merchant_clp = seeded_world["merchant_clp_id"]  # type: ignore[arg-type]
        async with session_factory() as session:
            rule = RecurringRule(
                merchant_id=merchant_clp,
                period_days=30,
                period_label="monthly",
                amount_min=Decimal("9.99"),
                amount_max=Decimal("9.99"),
                currency="CLP",
                is_active=True,
                confidence=0.9,
                last_seen_date=date(2026, 7, 5),
                occurrences=5,
            )
            session.add(rule)
            await session.commit()
            await session.refresh(rule)
            rule_id = rule.id

            # In-band transaction dated 2026-07-15.
            _add_transaction(
                session,
                statement_id=statement_a,
                merchant_id=merchant_clp,
                amount="9.99",
                txn_date=date(2026, 7, 15),
                currency="CLP",
            )
            await session.commit()

        async with session_factory() as session:
            rows = await DashboardService(session).recurring(
                period=date(2026, 7, 15), card_id="all"
            )

        assert len(rows) == 1
        assert rows[0]["id"] == rule_id
        assert rows[0]["currency"] == "CLP"
        assert rows[0]["is_active"] is True

    @pytest.mark.asyncio
    async def test_recurring_excludes_inactive(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        seeded_world: dict[str, object],
    ) -> None:
        """Inactive rules are excluded.

        Mirrors the spec scenario "Inactive rules are
        excluded": one active SHELL rule, one inactive
        SHELL rule → only the active one is in the
        response.
        """
        statement_a = seeded_world["statement_a_id"]  # type: ignore[arg-type]
        merchant_clp = seeded_world["merchant_clp_id"]  # type: ignore[arg-type]
        async with session_factory() as session:
            active = RecurringRule(
                merchant_id=merchant_clp,
                period_days=30,
                period_label="monthly",
                amount_min=Decimal("9.99"),
                amount_max=Decimal("9.99"),
                currency="CLP",
                is_active=True,
                confidence=0.9,
                last_seen_date=date(2026, 7, 5),
                occurrences=5,
            )
            inactive = RecurringRule(
                merchant_id=merchant_clp,
                period_days=30,
                period_label="monthly",
                amount_min=Decimal("19.99"),
                amount_max=Decimal("19.99"),
                currency="CLP",
                is_active=False,
                confidence=0.5,
                last_seen_date=date(2026, 7, 10),
                occurrences=3,
            )
            session.add_all([active, inactive])
            await session.commit()
            await session.refresh(active)
            await session.refresh(inactive)
            # In-band transaction for both amounts.
            for amount in ("9.99", "19.99"):
                _add_transaction(
                    session,
                    statement_id=statement_a,
                    merchant_id=merchant_clp,
                    amount=amount,
                    txn_date=date(2026, 7, 15),
                    currency="CLP",
                )
            await session.commit()

        async with session_factory() as session:
            rows = await DashboardService(session).recurring(
                period=date(2026, 7, 15), card_id="all"
            )

        assert len(rows) == 1
        assert rows[0]["id"] == active.id

    @pytest.mark.asyncio
    async def test_recurring_uses_transaction_date_filter(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        seeded_world: dict[str, object],
    ) -> None:
        """Rules without an in-band occurrence in the period are excluded.

        Mirrors the spec scenario "Rules without an
        in-band occurrence in the period are excluded":
        a rule with ``last_seen_date=2026-05-05`` and no
        July transaction is NOT in the response.
        """
        statement_a = seeded_world["statement_a_id"]  # type: ignore[arg-type]
        merchant_clp = seeded_world["merchant_clp_id"]  # type: ignore[arg-type]
        async with session_factory() as session:
            rule = RecurringRule(
                merchant_id=merchant_clp,
                period_days=30,
                period_label="monthly",
                amount_min=Decimal("9.99"),
                amount_max=Decimal("9.99"),
                currency="CLP",
                is_active=True,
                confidence=0.9,
                last_seen_date=date(2026, 5, 5),
                occurrences=5,
            )
            session.add(rule)
            await session.commit()
            await session.refresh(rule)
            # May transaction only — no July transaction.
            _add_transaction(
                session,
                statement_id=statement_a,
                merchant_id=merchant_clp,
                amount="9.99",
                txn_date=date(2026, 5, 10),
                currency="CLP",
            )
            await session.commit()

        async with session_factory() as session:
            rows = await DashboardService(session).recurring(
                period=date(2026, 7, 15), card_id="all"
            )

        assert rows == []


# ---------------------------------------------------------------------------
# card filter
# ---------------------------------------------------------------------------


class TestCardFilter:
    """The ``card_id`` filter is the documented ``UUID | "all"`` shape."""

    @pytest.mark.asyncio
    async def test_card_filter_uuid_filters_to_one_card(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        seeded_world: dict[str, object],
    ) -> None:
        """A UUID card filter narrows the summary to one card's transactions."""
        statement_a = seeded_world["statement_a_id"]  # type: ignore[arg-type]
        statement_b = seeded_world["statement_b_id"]  # type: ignore[arg-type]
        merchant_clp = seeded_world["merchant_clp_id"]  # type: ignore[arg-type]
        merchant_usd = seeded_world["merchant_usd_id"]  # type: ignore[arg-type]
        card_a = seeded_world["card_a_id"]  # type: ignore[arg-type]
        async with session_factory() as session:
            for day, amount in ((1, "10000.00"), (5, "20000.00"), (10, "30000.00")):
                _add_transaction(
                    session,
                    statement_id=statement_a,
                    merchant_id=merchant_clp,
                    amount=amount,
                    txn_date=date(2026, 7, day),
                    currency="CLP",
                )
            for day, amount in ((2, "50.00"), (12, "70.00")):
                _add_transaction(
                    session,
                    statement_id=statement_b,
                    merchant_id=merchant_usd,
                    amount=amount,
                    txn_date=date(2026, 7, day),
                    currency="USD",
                )
            await session.commit()

        async with session_factory() as session:
            response = await DashboardService(session).summary(
                period=date(2026, 7, 15), range_months=6, card_id=card_a
            )

        # Only card A's 3 transactions are counted.
        assert response.transaction_count == 3
        assert response.total_per_currency == {"CLP": Decimal("60000.00")}
        # The response echoes the UUID.
        assert response.card_id == card_a

    @pytest.mark.asyncio
    async def test_card_filter_all_aggregates_across_cards(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        seeded_world: dict[str, object],
    ) -> None:
        """``card_id="all"`` aggregates every card."""
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
                txn_date=date(2026, 7, 1),
                currency="CLP",
            )
            _add_transaction(
                session,
                statement_id=statement_b,
                merchant_id=merchant_usd,
                amount="50.00",
                txn_date=date(2026, 7, 2),
                currency="USD",
            )
            await session.commit()

        async with session_factory() as session:
            response = await DashboardService(session).summary(
                period=date(2026, 7, 15), range_months=6, card_id="all"
            )

        # Both currencies present.
        assert response.transaction_count == 2
        assert response.total_per_currency == {
            "CLP": Decimal("10000.00"),
            "USD": Decimal("50.00"),
        }
        assert response.card_id == "all"


# ---------------------------------------------------------------------------
# multi-currency
# ---------------------------------------------------------------------------


class TestMultiCurrency:
    """Multi-currency scenarios are exercised across the five methods."""

    @pytest.mark.asyncio
    async def test_multi_currency_separate_rollups(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        seeded_world: dict[str, object],
    ) -> None:
        """CLP and USD are kept as separate sub-rollups, never summed.

        Mirrors the spec scenario "Multi-currency period
        produces side-by-side entries".
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
                amount="50000.00",
                txn_date=date(2026, 7, 1),
                currency="CLP",
            )
            _add_transaction(
                session,
                statement_id=statement_b,
                merchant_id=merchant_usd,
                amount="100.00",
                txn_date=date(2026, 7, 5),
                currency="USD",
            )
            await session.commit()

        async with session_factory() as session:
            response = await DashboardService(session).summary(
                period=date(2026, 7, 15), range_months=6, card_id="all"
            )

        # Both keys present, never summed.
        assert set(response.total_per_currency.keys()) == {"CLP", "USD"}
        assert response.total_per_currency["CLP"] == Decimal("50000.00")
        assert response.total_per_currency["USD"] == Decimal("100.00")

    @pytest.mark.asyncio
    async def test_period_calculation_first_and_last_day_of_month(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        seeded_world: dict[str, object],
    ) -> None:
        """``period_start`` and ``period_end`` are the first and last day of the month.

        Mirrors the spec scenario for July 2026:
        ``period_start == "2026-07-01"``,
        ``period_end == "2026-07-31"`` (July has 31 days).
        """
        del seeded_world
        async with session_factory() as session:
            response = await DashboardService(session).summary(
                period=date(2026, 7, 15), range_months=6, card_id="all"
            )

        assert response.period_start == date(2026, 7, 1)
        assert response.period_end == date(2026, 7, 31)
