"""Tests for the transactions list/filter API (Phase 2 PR #3).

Covers:

* ``GET /api/v1/transactions`` — the new ``category_id`` and
  ``uncategorized`` Query parameters and their parenthesized
  OR composition (no filter, category_id only, uncategorized
  only, both).
* ``PATCH /api/v1/transactions/{id}`` — Accept-header dispatch
  (text/html returns the partial ``<tr>``; default returns
  JSON TransactionResponse).

The tests run against a fresh in-memory SQLite database per
test (via the ``seeded_engine`` / ``seeded_client`` fixtures
defined locally) and the schema is created by
:func:`Base.metadata.create_all`. The 12 seeded categories
come from the same fixture as :mod:`tests.test_categories`
so the ordering matches ``GET /api/v1/categories``.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import date
from decimal import Decimal

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from app.db.engine import create_engine
from app.db.session import create_session_factory
from app.main import create_app
from app.models import Bank, Category, CreditCard, Statement, Transaction
from app.models.base import Base

# ---------------------------------------------------------------------------
# Fixtures (local — small subset of the ones in test_categories.py)
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def seeded_engine(test_settings) -> AsyncIterator[AsyncEngine]:
    """Yield a fresh engine with the schema and the 12 categories seeded.

    Mirrors the fixture in :mod:`tests.test_categories` so the
    category ordering matches what ``GET /api/v1/categories``
    returns.
    """
    engine: AsyncEngine = create_engine(test_settings)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        factory = create_session_factory(engine)
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


@pytest_asyncio.fixture
async def seeded_client(seeded_engine: AsyncEngine) -> AsyncIterator[AsyncClient]:
    """Yield an :class:`httpx.AsyncClient` wired to the seeded app."""
    from app.db.session import get_session

    app = create_app()
    factory = async_sessionmaker(seeded_engine, expire_on_commit=False)

    async def _override_session():  # type: ignore[no-untyped-def]
        async with factory() as session:
            yield session

    app.dependency_overrides[get_session] = _override_session
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac


async def _seed_diverse_transactions(engine: AsyncEngine) -> dict[str, uuid.UUID]:
    """Seed a small set of transactions covering the four filter branches.

    Returns a mapping of human-readable label -> transaction UUID
    so the tests can assert specific rows:

    * "tagged_food"      — category_id = Dining Out, low_confidence=False
    * "tagged_shopping"  — category_id = Shopping,   low_confidence=False
    * "untagged_null"    — category_id = None,       low_confidence=False
    * "untagged_lowconf" — category_id = None,       low_confidence=True
    * "tagged_travel"    — category_id = Travel,     low_confidence=False
    """
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        bank = Bank(
            name="filter_test_bank",
            display_name="Filter Test Bank",
            password_formula="rut_sin_dv",
        )
        card = CreditCard(
            bank=bank,
            card_number_masked="XXXX XXXX XXXX 8888",
            cardholder="FILTER USER",
            currency="CLP",
        )
        statement = Statement(
            credit_card=card,
            period_start=date(2026, 6, 1),
            period_end=date(2026, 6, 30),
            statement_date=date(2026, 7, 1),
            file_path="filter/test.pdf",
            file_hash="e" * 64,
        )
        session.add_all([bank, card, statement])
        await session.flush()

        # Look up the seeded categories by name.
        result = await session.execute(select(Category).order_by(Category.sort_order.asc()))
        by_name = {row.name: row for row in result.scalars().all()}
        food_id = by_name["Dining Out"].id
        shopping_id = by_name["Shopping"].id
        travel_id = by_name["Travel"].id

        rows: dict[str, Transaction] = {
            "tagged_food": Transaction(
                statement_id=statement.id,
                date=date(2026, 6, 5),
                description="FOOD-TX",
                amount=Decimal("100.00"),
                currency="CLP",
                category="Dining Out",
                category_id=food_id,
                low_confidence=False,
            ),
            "tagged_shopping": Transaction(
                statement_id=statement.id,
                date=date(2026, 6, 6),
                description="SHOP-TX",
                amount=Decimal("200.00"),
                currency="CLP",
                category="Shopping",
                category_id=shopping_id,
                low_confidence=False,
            ),
            "untagged_null": Transaction(
                statement_id=statement.id,
                date=date(2026, 6, 7),
                description="NULL-TX",
                amount=Decimal("300.00"),
                currency="CLP",
                category=None,
                category_id=None,
                low_confidence=False,
            ),
            "untagged_lowconf": Transaction(
                statement_id=statement.id,
                date=date(2026, 6, 8),
                description="LOWCONF-TX",
                amount=Decimal("400.00"),
                currency="CLP",
                category="Custom Label",
                category_id=None,
                low_confidence=True,
            ),
            "tagged_travel": Transaction(
                statement_id=statement.id,
                date=date(2026, 6, 9),
                description="TRAVEL-TX",
                amount=Decimal("500.00"),
                currency="CLP",
                category="Travel",
                category_id=travel_id,
                low_confidence=False,
            ),
        }
        session.add_all(rows.values())
        await session.commit()
        for _label, row in rows.items():
            await session.refresh(row)
        return {label: row.id for label, row in rows.items()}


def _descriptions(payload: list[dict[str, object]]) -> list[str]:
    """Return the ``description`` field of every transaction in the payload."""
    return [str(row["description"]) for row in payload]


# ---------------------------------------------------------------------------
# GET /api/v1/transactions — list filter branches
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_transactions_no_category_filter(
    seeded_engine: AsyncEngine, seeded_client: AsyncClient
) -> None:
    """No category filter returns every transaction.

    Baseline: with no filter the response is the full set of
    seeded rows (5 transactions). This is the regression
    guard — every other filter combination must shrink the
    set, never expand it.
    """
    await _seed_diverse_transactions(seeded_engine)
    response = await seeded_client.get("/api/v1/transactions")
    assert response.status_code == 200
    body = response.json()
    assert len(body) == 5
    descriptions = set(_descriptions(body))
    assert descriptions == {"FOOD-TX", "SHOP-TX", "NULL-TX", "LOWCONF-TX", "TRAVEL-TX"}


@pytest.mark.asyncio
async def test_list_transactions_category_id_filter(
    seeded_engine: AsyncEngine, seeded_client: AsyncClient
) -> None:
    """``?category_id=<uuid>`` returns only transactions with that category.

    Single UUID: only ``FOOD-TX`` (Dining Out) and ``TRAVEL-TX``
    (Travel) match. The other three (Shopping — wrong UUID,
    NULL, low_confidence) are excluded.
    """
    factory = async_sessionmaker(seeded_engine, expire_on_commit=False)
    async with factory() as session:
        result = await session.execute(select(Category).order_by(Category.sort_order.asc()))
        by_name = {row.name: row for row in result.scalars().all()}
    food_id = by_name["Dining Out"].id
    travel_id = by_name["Travel"].id

    await _seed_diverse_transactions(seeded_engine)
    response = await seeded_client.get(
        "/api/v1/transactions",
        params=[("category_id", str(food_id)), ("category_id", str(travel_id))],
    )
    assert response.status_code == 200
    body = response.json()
    assert set(_descriptions(body)) == {"FOOD-TX", "TRAVEL-TX"}


@pytest.mark.asyncio
async def test_list_transactions_uncategorized_filter(
    seeded_engine: AsyncEngine, seeded_client: AsyncClient
) -> None:
    """``?uncategorized=true`` returns NULL or low_confidence=True rows.

    Only the NULL row (``NULL-TX``) and the low_confidence
    row (``LOWCONF-TX``) match. The three tagged rows are
    excluded.
    """
    await _seed_diverse_transactions(seeded_engine)
    response = await seeded_client.get("/api/v1/transactions", params={"uncategorized": "true"})
    assert response.status_code == 200
    body = response.json()
    assert set(_descriptions(body)) == {"NULL-TX", "LOWCONF-TX"}


@pytest.mark.asyncio
async def test_list_transactions_both_filters(
    seeded_engine: AsyncEngine, seeded_client: AsyncClient
) -> None:
    """``?category_id=<uuid>&uncategorized=true`` returns the union.

    Parenthesized OR: the food row matches via category_id,
    the NULL and low_confidence rows match via the
    uncategorized branch. The Shopping and Travel rows are
    excluded (not in the UUID set, not NULL/low_confidence).
    """
    factory = async_sessionmaker(seeded_engine, expire_on_commit=False)
    async with factory() as session:
        result = await session.execute(select(Category).order_by(Category.sort_order.asc()))
        by_name = {row.name: row for row in result.scalars().all()}
    food_id = by_name["Dining Out"].id

    await _seed_diverse_transactions(seeded_engine)
    response = await seeded_client.get(
        "/api/v1/transactions",
        params={"category_id": str(food_id), "uncategorized": "true"},
    )
    assert response.status_code == 200
    body = response.json()
    assert set(_descriptions(body)) == {"FOOD-TX", "NULL-TX", "LOWCONF-TX"}


# ---------------------------------------------------------------------------
# PATCH /api/v1/transactions/{id} — Accept header dispatch
# ---------------------------------------------------------------------------


async def _seed_single_tx_for_patch(engine: AsyncEngine) -> uuid.UUID:
    """Seed one untagged transaction. Returns its UUID.

    The transaction is left untagged (no ``category``, no
    ``category_id``) so the PATCH tests can drive both the
    ``category_id`` and the legacy ``category`` paths from a
    clean starting point.
    """
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        bank = Bank(
            name="patch_dispatch_bank",
            display_name="Patch Dispatch Bank",
            password_formula="rut_sin_dv",
        )
        card = CreditCard(
            bank=bank,
            card_number_masked="XXXX XXXX XXXX 9999",
            cardholder="DISPATCH USER",
            currency="CLP",
        )
        statement = Statement(
            credit_card=card,
            period_start=date(2026, 5, 1),
            period_end=date(2026, 5, 31),
            statement_date=date(2026, 6, 1),
            file_path="dispatch/test.pdf",
            file_hash="f" * 64,
        )
        tx = Transaction(
            statement=statement,
            date=date(2026, 5, 5),
            description="DISPATCH-TX",
            amount=Decimal("500.00"),
            currency="CLP",
        )
        session.add_all([bank, card, statement, tx])
        await session.commit()
        return tx.id


@pytest.mark.asyncio
async def test_patch_with_accept_text_html_returns_html_partial(
    seeded_engine: AsyncEngine, seeded_client: AsyncClient
) -> None:
    """``Accept: text/html`` returns the partial ``<tr>`` row with the new pick selected.

    The HTMX swap path: the response is HTML, not JSON. The
    markup contains the new ``<option ... selected>`` so the
    picker reflects the just-applied PATCH. The response is
    an ``<tr>`` element (the partial template's row format),
    not a JSON object.
    """
    tx_id = await _seed_single_tx_for_patch(seeded_engine)
    factory = async_sessionmaker(seeded_engine, expire_on_commit=False)
    async with factory() as session:
        result = await session.execute(select(Category).order_by(Category.sort_order.asc()))
        groceries_id = next(row.id for row in result.scalars().all() if row.name == "Groceries")

    response = await seeded_client.patch(
        f"/api/v1/transactions/{tx_id}",
        json={"category_id": str(groceries_id)},
        headers={"Accept": "text/html"},
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    body = response.text
    # The partial is a single <tr> with the new pick marked
    # as the selected option. The hx-patch / hx-target
    # attributes are preserved so a follow-up swap still
    # works.
    assert "<tr" in body
    assert 'data-testid="category-select"' in body
    assert 'hx-patch="/api/v1/transactions/' in body
    # The new pick is selected; the row description is in
    # the markup so the user can see the right row was
    # re-rendered.
    assert "DISPATCH-TX" in body
    assert 'value="' + str(groceries_id) + '"' in body
    assert "selected" in body


@pytest.mark.asyncio
async def test_patch_with_accept_application_json_returns_json(
    seeded_engine: AsyncEngine, seeded_client: AsyncClient
) -> None:
    """Default ``Accept: application/json`` (or no header) returns TransactionResponse.

    The JSON contract is preserved: the response is a
    TransactionResponse with the new ``category_id``,
    ``category``, and ``low_confidence=False``. The
    HTML branch is opt-in via the Accept header.
    """
    tx_id = await _seed_single_tx_for_patch(seeded_engine)
    factory = async_sessionmaker(seeded_engine, expire_on_commit=False)
    async with factory() as session:
        result = await session.execute(select(Category).order_by(Category.sort_order.asc()))
        groceries_id = next(row.id for row in result.scalars().all() if row.name == "Groceries")

    # No Accept header at all — the default path.
    response = await seeded_client.patch(
        f"/api/v1/transactions/{tx_id}",
        json={"category_id": str(groceries_id)},
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    body = response.json()
    assert body["category_id"] == str(groceries_id)
    assert body["category"] == "Groceries"
    assert body["low_confidence"] is False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


# Local re-export so the imports above read top-to-bottom.
from sqlalchemy import select  # noqa: E402

__all__ = [
    "seeded_client",
    "seeded_engine",
    "test_list_transactions_both_filters",
    "test_list_transactions_category_id_filter",
    "test_list_transactions_no_category_filter",
    "test_list_transactions_uncategorized_filter",
    "test_patch_with_accept_application_json_returns_json",
    "test_patch_with_accept_text_html_returns_html_partial",
]
