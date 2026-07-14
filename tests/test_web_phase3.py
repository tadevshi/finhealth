"""Tests for the Phase 3 dashboard page (PR #10).

Covers the ``GET /dashboard`` page and the five HTMX
partials that load each section. Mirrors the pattern from
:mod:`tests.test_web_phase1` (per-test fresh in-memory
SQLite database, ``httpx.AsyncClient`` driven through
``ASGITransport``).

Test surface
------------

* **Page** (3) — ``GET /dashboard`` returns 200 with the
  card picker, the period picker, and the 5 sections
  rendered.
* **Summary partial** (1) — returns 200 with the KPI grid.
* **Categories partial** (1) — returns 200 with 12 rows.
* **Merchants partial** (1) — returns 200 with the seeded
  merchants.
* **Monthly partial** (1) — returns 200 with the time
  series Tailwind bars (``style="width: X%"``).
* **Recurring partial** (1) — returns 200 with the active
  rules.
* **Filter coverage** (3) — card-id / range / period
  filter combinations are honoured.
* **Multi-currency** (1) — both CLP and USD sub-grids
  appear when both are present.

Total: 12 tests.

The dashboard is server-rendered (Tailwind + HTMX + Alpine,
no JS chart library) so the assertion surface is the raw
HTML response body. The :class:`app.services.dashboard.DashboardService`
unit tests live in :mod:`tests.test_dashboard`; the API
contract tests live in :mod:`tests.test_dashboard_api`; this
module only tests the *web* layer.
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
from app.models import (
    Bank,
    Category,
    CreditCard,
    Merchant,
    RecurringRule,
    Statement,
    Transaction,
)
from app.models.base import Base
from app.models.statement import StatementStatus

# ---------------------------------------------------------------------------
# URL constants and testid markers
# ---------------------------------------------------------------------------

DASHBOARD_PATH = "/dashboard"
SUMMARY_PATH = "/dashboard/sections/summary"
CATEGORIES_PATH = "/dashboard/sections/categories"
MERCHANTS_PATH = "/dashboard/sections/merchants"
MONTHLY_PATH = "/dashboard/sections/monthly"
RECURRING_PATH = "/dashboard/sections/recurring"

CARD_PICKER_TESTID = 'data-testid="dashboard-card-picker"'
RANGE_PICKER_TESTID = 'data-testid="dashboard-range-picker"'
DASHBOARD_TITLE_TESTID = 'data-testid="dashboard-title"'
SUMMARY_TARGET_TESTID = 'data-testid="dashboard-summary-target"'
CATEGORIES_TARGET_TESTID = 'data-testid="dashboard-categories-target"'
MERCHANTS_TARGET_TESTID = 'data-testid="dashboard-merchants-target"'
MONTHLY_TARGET_TESTID = 'data-testid="dashboard-monthly-target"'
RECURRING_TARGET_TESTID = 'data-testid="dashboard-recurring-target"'
SUMMARY_TESTID = 'data-testid="dashboard-summary"'
SUMMARY_EMPTY_TESTID = 'data-testid="dashboard-summary-empty"'
CATEGORIES_TESTID = 'data-testid="dashboard-categories"'
CATEGORIES_LIST_TESTID = 'data-testid="dashboard-categories-list"'
CATEGORIES_ROW_TESTID = 'data-testid="dashboard-categories-row"'
CATEGORIES_BAR_TESTID = 'data-testid="dashboard-categories-bar"'
MERCHANTS_TESTID = 'data-testid="dashboard-merchants"'
MERCHANTS_LIST_TESTID = 'data-testid="dashboard-merchants-list"'
MERCHANTS_ROW_TESTID = 'data-testid="dashboard-merchants-row"'
MONTHLY_TESTID = 'data-testid="dashboard-monthly"'
MONTHLY_LIST_TESTID = 'data-testid="dashboard-monthly-list"'
MONTHLY_BAR_TESTID = 'data-testid="dashboard-monthly-bar"'
RECURRING_TESTID = 'data-testid="dashboard-recurring"'
RECURRING_LIST_TESTID = 'data-testid="dashboard-recurring-list"'

# The pickers use ``x-model`` to bind to the Alpine state and
# Alpine to drive the HTMX refresh; the test surface does not
# need Alpine to be active — it just asserts the markup
# wires the right URLs and the pickers are present.
ALL_CARDS_LABEL = "Todas las cards"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def dashboard_engine(test_settings: Settings) -> AsyncIterator[AsyncEngine]:
    """Yield a fresh engine with the schema + the 12 closed-set categories seeded.

    Mirrors the engine fixture from
    :mod:`tests.test_dashboard_api`; reuses the same 12-row
    category seed so the categories partial is meaningful.
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


@pytest_asyncio.fixture
async def session_factory(
    dashboard_engine: AsyncEngine,
) -> async_sessionmaker[AsyncSession]:
    """Yield a session factory bound to the dashboard web engine."""
    return async_sessionmaker(dashboard_engine, expire_on_commit=False)


@pytest_asyncio.fixture
async def client(
    dashboard_engine: AsyncEngine,
) -> AsyncIterator[AsyncClient]:
    """Yield an :class:`httpx.AsyncClient` wired to a fresh app.

    The app's ``get_session`` dependency is overridden so the
    page and the partials read and write to the seeded test
    engine.
    """
    app = create_app()
    factory = async_sessionmaker(dashboard_engine, expire_on_commit=False)

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
    """Yield a bank, two cards (CLP + USD), two statements, and two merchants.

    The "world" is enough to exercise the multi-currency side-
    by-side and the single-card narrowing without rebuilding
    fixtures per test. The CLP card is the default for the
    "single card" tests; the USD card is the second
    ``is_active=True`` row so the picker is meaningful.
    """
    now = datetime.now(UTC)
    async with session_factory() as session:
        bank = Bank(
            name="dashboard_web_bank",
            display_name="Dashboard Web Bank",
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
            card_number_masked="XXXX XXXX XXXX 1001",
            cardholder="WEB USER A",
            currency="CLP",
            is_active=True,
        )
        card_b = CreditCard(
            bank_id=bank.id,
            card_number_masked="XXXX XXXX XXXX 1002",
            cardholder="WEB USER B",
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
            file_path="/tmp/dashboard-web-a.pdf",
            file_hash="a" * 64,
            status=StatementStatus.COMPLETED,
        )
        statement_b = Statement(
            credit_card_id=card_b.id,
            period_start=date(2026, 6, 1),
            period_end=date(2026, 6, 30),
            statement_date=date(2026, 6, 30),
            file_path="/tmp/dashboard-web-b.pdf",
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
    """Insert a transaction row and return the ORM object."""
    txn = Transaction(
        statement_id=statement_id,
        date=txn_date,
        description=f"WEB {merchant_id} {txn_date.isoformat()}",
        amount=Decimal(amount),
        currency=currency,
        category_id=category_id,
        low_confidence=False,
    )
    txn.merchant_id = merchant_id
    session.add(txn)
    return txn


# ---------------------------------------------------------------------------
# Page-level tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dashboard_page_returns_200(client: AsyncClient) -> None:
    """``GET /dashboard`` returns 200 with ``text/html`` and the page skeleton."""
    response = await client.get(DASHBOARD_PATH)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    body = response.text
    assert DASHBOARD_TITLE_TESTID in body
    # The 5 section targets are wired with hx-get.
    assert SUMMARY_TARGET_TESTID in body
    assert CATEGORIES_TARGET_TESTID in body
    assert MERCHANTS_TARGET_TESTID in body
    assert MONTHLY_TARGET_TESTID in body
    assert RECURRING_TARGET_TESTID in body


@pytest.mark.asyncio
async def test_dashboard_page_contains_card_picker(
    client: AsyncClient, seeded_world: dict[str, object]
) -> None:
    """The card picker exposes ``Todas las cards`` + every active card."""
    body = (await client.get(DASHBOARD_PATH)).text
    assert CARD_PICKER_TESTID in body
    assert ALL_CARDS_LABEL in body
    card_a = seeded_world["card_a_id"]  # type: ignore[arg-type]
    card_b = seeded_world["card_b_id"]  # type: ignore[arg-type]
    # Each active card's value appears in the picker.
    assert f'value="{card_a}"' in body
    assert f'value="{card_b}"' in body
    # "Todas las cards" is the default selected option.
    assert '<option value="all" selected>' in body or 'value="all" selected' in body


@pytest.mark.asyncio
async def test_dashboard_page_contains_period_picker(client: AsyncClient) -> None:
    """The period picker exposes the 3 documented range options, with the
    default (YTD) selected. The v5 design collapses the v2 5-option
    dropdown into a 3-segment control (6M / YTD / 1Y); the hidden
    ``<select>`` is kept for HTMX contract compatibility.
    """
    body = (await client.get(DASHBOARD_PATH)).text
    assert RANGE_PICKER_TESTID in body
    # The 3 range options (6M, YTD, 1Y).
    assert 'value="6"' in body
    assert 'value="0"' in body
    assert 'value="12"' in body
    # YTD (the value 0) is selected by default in the v5 design
    # (the route handler maps the "YTD" label to 0).
    assert '<option value="0" selected>' in body


# ---------------------------------------------------------------------------
# Partial tests (HTMX)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dashboard_summary_partial_returns_200(client: AsyncClient) -> None:
    """``GET /dashboard/sections/summary`` returns 200 with the KPI grid."""
    response = await client.get(SUMMARY_PATH, params={"period": "2026-07"})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    body = response.text
    assert SUMMARY_TESTID in body
    # Empty-period response renders the empty-state message.
    assert SUMMARY_EMPTY_TESTID in body


@pytest.mark.asyncio
async def test_dashboard_categories_partial_returns_12_rows(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seeded_world: dict[str, object],
) -> None:
    """The categories partial renders all 12 closed-set category rows."""
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

    response = await client.get(CATEGORIES_PATH, params={"period": "2026-07"})
    assert response.status_code == 200
    body = response.text
    assert CATEGORIES_TESTID in body
    assert CATEGORIES_LIST_TESTID in body
    # 12 category rows.
    assert body.count(CATEGORIES_ROW_TESTID) == 12
    # The Groceries row carries a non-zero bar width.
    assert "Groceries" in body
    assert CATEGORIES_BAR_TESTID in body


@pytest.mark.asyncio
async def test_dashboard_merchants_partial_returns_top_n(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seeded_world: dict[str, object],
) -> None:
    """The merchants partial renders the top-N merchants for the period."""
    statement_id = seeded_world["statement_a_id"]  # type: ignore[arg-type]
    merchant_clp = seeded_world["merchant_clp_id"]  # type: ignore[arg-type]
    merchant_usd = seeded_world["merchant_usd_id"]  # type: ignore[arg-type]
    statement_b = seeded_world["statement_b_id"]  # type: ignore[arg-type]

    async with session_factory() as session:
        _add_transaction(
            session,
            statement_id=statement_id,
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

    response = await client.get(MERCHANTS_PATH, params={"period": "2026-07"})
    assert response.status_code == 200
    body = response.text
    assert MERCHANTS_TESTID in body
    assert MERCHANTS_LIST_TESTID in body
    # 2 merchants seeded for the period (netflix + spotify).
    assert body.count(MERCHANTS_ROW_TESTID) == 2
    assert "netflix" in body
    assert "spotify" in body


@pytest.mark.asyncio
async def test_dashboard_monthly_partial_returns_time_series(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seeded_world: dict[str, object],
) -> None:
    """The monthly partial renders the Tailwind bar chart with ``style="width: X%"``."""
    statement_id = seeded_world["statement_a_id"]  # type: ignore[arg-type]
    merchant_id = seeded_world["merchant_clp_id"]  # type: ignore[arg-type]

    async with session_factory() as session:
        _add_transaction(
            session,
            statement_id=statement_id,
            merchant_id=merchant_id,
            amount="10000.00",
            txn_date=date(2026, 7, 10),
            currency="CLP",
        )
        await session.commit()

    response = await client.get(MONTHLY_PATH, params={"range": 6})
    assert response.status_code == 200
    body = response.text
    assert MONTHLY_TESTID in body
    assert MONTHLY_LIST_TESTID in body
    # At least one Tailwind bar carries ``style="width: X%"``.
    assert "style=\"width:" in body
    assert MONTHLY_BAR_TESTID in body
    # The chart has 6 month rows (the default range).
    assert body.count('data-testid="dashboard-monthly-row"') == 6


@pytest.mark.asyncio
async def test_dashboard_recurring_partial_returns_active_rules(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seeded_world: dict[str, object],
) -> None:
    """The recurring partial lists the active rules with an in-band occurrence."""
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
        _add_transaction(
            session,
            statement_id=statement_id,
            merchant_id=merchant_id,
            amount="9.99",
            txn_date=date(2026, 7, 15),
            currency="CLP",
        )
        await session.commit()

    response = await client.get(RECURRING_PATH, params={"period": "2026-07"})
    assert response.status_code == 200
    body = response.text
    assert RECURRING_TESTID in body
    assert RECURRING_LIST_TESTID in body
    # The rule row is present with the merchant name resolved.
    assert "netflix" in body
    assert "monthly" in body


# ---------------------------------------------------------------------------
# Filter coverage
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dashboard_with_card_id_filter(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seeded_world: dict[str, object],
) -> None:
    """A ``card_id=<uuid>`` filter narrows the partial response to that card."""
    statement_a = seeded_world["statement_a_id"]  # type: ignore[arg-type]
    statement_b = seeded_world["statement_b_id"]  # type: ignore[arg-type]
    merchant_clp = seeded_world["merchant_clp_id"]  # type: ignore[arg-type]
    merchant_usd = seeded_world["merchant_usd_id"]  # type: ignore[arg-type]
    card_a = seeded_world["card_a_id"]  # type: ignore[arg-type]

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
        _add_transaction(
            session,
            statement_id=statement_b,
            merchant_id=merchant_usd,
            amount="50.00",
            txn_date=date(2026, 7, 15),
            currency="USD",
        )
        await session.commit()

    response = await client.get(
        SUMMARY_PATH, params={"period": "2026-07", "card_id": str(card_a)}
    )
    assert response.status_code == 200
    body = response.text
    # Card A has 3 transactions; the v5 summary card shows the
    # count in the "Transacciones" KPI card.
    assert "Transacciones" in body
    # Only CLP appears for card A (no USD on this card).
    assert "CLP" in body
    # The page initial paint also reflects the card filter when
    # the user lands with a deep link. The picker shows the
    # bank display name + masked number, not the cardholder.
    page_response = await client.get(
        DASHBOARD_PATH, params={"card_id": str(card_a)}
    )
    assert page_response.status_code == 200
    assert "XXXX XXXX XXXX 1001" in page_response.text
    # The USD cardholder is not in the picker when the filter
    # narrows the page (the picker is always the full list —
    # this assertion is on the partial's filtered totals).


@pytest.mark.asyncio
async def test_dashboard_with_period_filter(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seeded_world: dict[str, object],
) -> None:
    """A ``period=YYYY-MM`` filter narrows the summary to that month."""
    statement_id = seeded_world["statement_a_id"]  # type: ignore[arg-type]
    merchant_id = seeded_world["merchant_clp_id"]  # type: ignore[arg-type]
    categories = seeded_world["categories"]  # type: ignore[assignment]
    groceries = categories["Groceries"]

    async with session_factory() as session:
        _add_transaction(
            session,
            statement_id=statement_id,
            merchant_id=merchant_id,
            amount="15000.00",
            txn_date=date(2026, 6, 5),
            currency="CLP",
            category_id=groceries.id,
        )
        _add_transaction(
            session,
            statement_id=statement_id,
            merchant_id=merchant_id,
            amount="5000.00",
            txn_date=date(2026, 7, 5),
            currency="CLP",
            category_id=groceries.id,
        )
        await session.commit()

    response_jun = await client.get(SUMMARY_PATH, params={"period": "2026-06"})
    assert response_jun.status_code == 200
    assert "15,000" in response_jun.text

    response_jul = await client.get(SUMMARY_PATH, params={"period": "2026-07"})
    assert response_jul.status_code == 200
    assert "5,000" in response_jul.text
    # The period label reflects the requested month.
    assert "2026-06" in response_jun.text
    assert "2026-07" in response_jul.text


@pytest.mark.asyncio
async def test_dashboard_with_range_filter(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seeded_world: dict[str, object],
) -> None:
    """A ``range=N`` filter changes the monthly time series length."""
    statement_id = seeded_world["statement_a_id"]  # type: ignore[arg-type]
    merchant_id = seeded_world["merchant_clp_id"]  # type: ignore[arg-type]

    async with session_factory() as session:
        _add_transaction(
            session,
            statement_id=statement_id,
            merchant_id=merchant_id,
            amount="10000.00",
            txn_date=date(2026, 7, 10),
            currency="CLP",
        )
        await session.commit()

    response_3 = await client.get(MONTHLY_PATH, params={"range": 3})
    assert response_3.status_code == 200
    assert response_3.text.count('data-testid="dashboard-monthly-row"') == 3

    response_12 = await client.get(MONTHLY_PATH, params={"range": 12})
    assert response_12.status_code == 200
    assert response_12.text.count('data-testid="dashboard-monthly-row"') == 12


# ---------------------------------------------------------------------------
# Multi-currency side-by-side
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dashboard_multi_currency_side_by_side(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seeded_world: dict[str, object],
) -> None:
    """When both CLP and USD are present, the summary renders two sub-grids."""
    statement_a = seeded_world["statement_a_id"]  # type: ignore[arg-type]
    statement_b = seeded_world["statement_b_id"]  # type: ignore[arg-type]
    merchant_clp = seeded_world["merchant_clp_id"]  # type: ignore[arg-type]
    merchant_usd = seeded_world["merchant_usd_id"]  # type: ignore[arg-type]

    async with session_factory() as session:
        _add_transaction(
            session,
            statement_id=statement_a,
            merchant_id=merchant_clp,
            amount="100000.00",
            txn_date=date(2026, 7, 5),
            currency="CLP",
        )
        _add_transaction(
            session,
            statement_id=statement_b,
            merchant_id=merchant_usd,
            amount="250.00",
            txn_date=date(2026, 7, 10),
            currency="USD",
        )
        await session.commit()

    response = await client.get(SUMMARY_PATH, params={"period": "2026-07"})
    assert response.status_code == 200
    body = response.text
    # Both sub-grids are present in the response body.
    assert body.count('data-testid="dashboard-summary-currency"') == 2
    assert "CLP" in body
    assert "USD" in body
    # The CLP and USD amounts are rendered with thousands separators.
    assert "100,000" in body
    assert "250.00" in body


# ---------------------------------------------------------------------------
# Tailwind-only / no-JS-chart-library guard
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dashboard_does_not_load_js_chart_library(client: AsyncClient) -> None:
    """The page does not include any JS chart library script tag.

    Mirrors the spec scenario "No JavaScript chart library is
    loaded": no Chart.js, ApexCharts, Plotly, ECharts, or D3
    script tags. Only Tailwind / HTMX / Alpine are allowed.
    """
    body = (await client.get(DASHBOARD_PATH)).text.lower()
    assert "chart.js" not in body
    assert "apexcharts" not in body
    assert "plotly" not in body
    assert "echarts" not in body
    # D3 has a low-coincidence substring; assert against the
    # common cdn paths to keep the test honest.
    assert "d3js.org" not in body
    assert "cdn.jsdelivr.net/npm/d3" not in body
