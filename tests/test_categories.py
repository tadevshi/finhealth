"""Tests for the categories foundation (Phase 2, PR #2).

Covers:

* the ``Category`` ORM model (round-trip + name uniqueness);
* the ``GET /api/v1/categories`` endpoint (returns 12 rows in
  ``sort_order`` ascending);
* the ``POST /api/v1/categories/{id}`` rename endpoint (happy
  path, 404, 422 collision, atomicity rollback);
* the ``PATCH /api/v1/transactions/{id}`` ``category_id``
  write-through;
* the ``PATCH /api/v1/transactions/{id}`` legacy ``category``
  path emits exactly one deprecation log line.

The tests run against a fresh in-memory SQLite database per
test (via the ``client`` fixture from :mod:`tests.conftest`)
and the schema is created by the same :func:`Base.metadata.create_all`
call the production app uses at startup. The migration
round-trips (so the seed of 12 categories) are exercised by
:mod:`tests.test_alembic`; the unit tests here only care about
the API contract, not the migration shape.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncIterator
from datetime import date
from decimal import Decimal

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from app.db.engine import create_engine
from app.db.session import create_session_factory
from app.main import create_app
from app.models import Bank, Category, CreditCard, Statement, Transaction
from app.models.base import Base

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def seeded_engine(test_settings) -> AsyncIterator[AsyncEngine]:
    """Yield a fresh engine with the schema created and the 12 categories seeded.

    The 12 categories are inserted in ``sort_order`` so a
    ``GET /api/v1/categories`` test can assert the exact
    order. The schema is created by
    :func:`Base.metadata.create_all` so the test surface
    matches what the production app sees at startup.
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
    """Yield an ``httpx.AsyncClient`` wired to the seeded app.

    The app is created with the test settings, but its
    session factory is overridden to point at the seeded
    engine so the in-memory database and its seed are
    visible to the request handlers.
    """
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


# ---------------------------------------------------------------------------
# Category model
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_category_round_trips_with_uuid_and_timestamps(
    seeded_engine: AsyncEngine,
) -> None:
    """``Category`` persists with auto-generated ``id`` and timestamps."""
    factory = async_sessionmaker(seeded_engine, expire_on_commit=False)
    async with factory() as session:
        category = Category(
            name="pet_stores",
            display_name="Pet Stores",
            sort_order=99,
        )
        session.add(category)
        await session.commit()
        await session.refresh(category)

    assert isinstance(category.id, uuid.UUID)
    assert category.name == "pet_stores"
    assert category.display_name == "Pet Stores"
    assert category.sort_order == 99
    assert isinstance(category.created_at, type(category.updated_at))
    assert category.created_at <= category.updated_at


@pytest.mark.asyncio
async def test_category_name_must_be_unique(seeded_engine: AsyncEngine) -> None:
    """Two categories with the same ``name`` violate the unique constraint.

    The 12 seeded rows already cover the canonical names;
    a second insert for any of them hits the unique
    index. The test uses ``Dining Out`` (the first seed)
    as the conflicting name.
    """
    from sqlalchemy.exc import IntegrityError

    factory = async_sessionmaker(seeded_engine, expire_on_commit=False)
    async with factory() as session:
        # First commit is a no-op — the seeded row is already
        # there. The ``add`` is the second row, which violates
        # the unique constraint on the next commit.
        session.add(
            Category(
                name="Dining Out",
                display_name="Food (different)",
                sort_order=100,
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()


# ---------------------------------------------------------------------------
# GET /api/v1/categories
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_categories_returns_twelve_in_sort_order(
    seeded_client: AsyncClient,
) -> None:
    """``GET /api/v1/categories`` returns the 12 seeded rows in ``sort_order``."""
    response = await seeded_client.get("/api/v1/categories")
    assert response.status_code == 200
    payload = response.json()
    assert len(payload) == 12
    # The 12 names are returned in the canonical sort_order
    # order (1..12). The test asserts both the *order* and
    # the *names* in one pass.
    names = [row["name"] for row in payload]
    assert names == [
        "Dining Out",
        "Groceries",
        "Transportation",
        "Shopping",
        "Entertainment",
        "Bills",
        "Health",
        "Travel",
        "Subscriptions",
        "Personal Care",
        "Uncategorized",
        "Other",
    ]
    # Every row carries the expected response shape.
    for row in payload:
        assert set(row.keys()) == {
            "id",
            "name",
            "display_name",
            "sort_order",
            "created_at",
            "updated_at",
        }
        uuid.UUID(row["id"])  # parses cleanly


# ---------------------------------------------------------------------------
# POST /api/v1/categories/{id} — rename
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rename_category_happy_path_propagates_to_transactions(
    seeded_engine: AsyncEngine, seeded_client: AsyncClient
) -> None:
    """Renaming a category updates its name and the denormalized string on every tx.

    The test seeds 3 transactions that point at ``Dining Out``
    and renames ``Dining Out`` to ``Dining``. The response
    carries the new name and the 3 transactions' ``category``
    column is updated to ``"Dining"`` in the same commit.
    """
    factory = async_sessionmaker(seeded_engine, expire_on_commit=False)
    food_id: uuid.UUID
    statement_id: uuid.UUID
    async with factory() as session:
        # Find the seeded Dining Out row.
        result = await session.execute(select(Category).where(Category.name == "Dining Out"))
        food = result.scalar_one()
        food_id = food.id

        # Seed a bank + card + statement so transactions have
        # a valid parent chain.
        bank = Bank(
            name="rename_test_bank",
            display_name="Rename Test Bank",
            password_formula="rut_sin_dv",
        )
        card = CreditCard(
            bank=bank,
            card_number_masked="XXXX XXXX XXXX 9999",
            cardholder="RENAME USER",
            currency="CLP",
        )
        statement = Statement(
            credit_card=card,
            period_start=date(2026, 5, 1),
            period_end=date(2026, 5, 31),
            statement_date=date(2026, 6, 1),
            file_path="rename/test.pdf",
            file_hash="a" * 64,
        )
        # 3 transactions tagged "Dining Out" (the legacy denormalized
        # string) with the Dining Out FK.
        txns = [
            Transaction(
                statement=statement,
                date=date(2026, 5, 5),
                description="TX-1",
                amount=Decimal("100.00"),
                currency="CLP",
                category="Dining Out",
                category_id=food.id,
                low_confidence=False,
            ),
            Transaction(
                statement=statement,
                date=date(2026, 5, 6),
                description="TX-2",
                amount=Decimal("200.00"),
                currency="CLP",
                category="Dining Out",
                category_id=food.id,
                low_confidence=False,
            ),
            Transaction(
                statement=statement,
                date=date(2026, 5, 7),
                description="TX-3",
                amount=Decimal("300.00"),
                currency="CLP",
                category="Dining Out",
                category_id=food.id,
                low_confidence=False,
            ),
        ]
        session.add_all([bank, card, statement, *txns])
        await session.commit()
        statement_id = statement.id

    response = await seeded_client.post(
        f"/api/v1/categories/{food_id}",
        json={"name": "Dining", "display_name": "Dining & Eating Out"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["name"] == "Dining"
    assert body["display_name"] == "Dining & Eating Out"
    assert uuid.UUID(body["id"]) == food_id

    # Re-read the 3 transactions and assert the denormalized
    # string was propagated atomically.
    async with factory() as session:
        txns = (
            (
                await session.execute(
                    select(Transaction)
                    .where(Transaction.statement_id == statement_id)
                    .order_by(Transaction.date.asc())
                )
            )
            .scalars()
            .all()
        )
        for tx in txns:
            assert tx.category == "Dining"
            assert tx.category_id == food_id


@pytest.mark.asyncio
async def test_rename_category_404_on_unknown_uuid(
    seeded_client: AsyncClient,
) -> None:
    """An unknown category UUID returns 404, no rows touched."""
    unknown = uuid.uuid4()
    response = await seeded_client.post(
        f"/api/v1/categories/{unknown}",
        json={"name": "Anything"},
    )
    assert response.status_code == 404
    assert str(unknown) in response.json()["detail"]


@pytest.mark.asyncio
async def test_rename_category_422_on_name_collision(
    seeded_client: AsyncClient,
) -> None:
    """Renaming a category to another row's ``name`` returns 422.

    The endpoint checks the proposed ``name`` against every
    *other* row before the UPDATE; a collision surfaces as
    422 with a clear error, not a 500 from the unique
    constraint.
    """
    # Find the seeded Dining Out and Transportation rows.
    (food_id,) = await _find_category_ids(seeded_client, "Dining Out")

    response = await seeded_client.post(
        f"/api/v1/categories/{food_id}",
        json={"name": "Transportation"},
    )
    assert response.status_code == 422
    assert "Transportation" in response.json()["detail"]


@pytest.mark.asyncio
async def test_rename_category_422_on_empty_body(
    seeded_client: AsyncClient,
) -> None:
    """A body with neither ``name`` nor ``display_name`` returns 422."""
    (food_id,) = await _find_category_ids(seeded_client, "Dining Out")
    response = await seeded_client.post(
        f"/api/v1/categories/{food_id}",
        json={},
    )
    assert response.status_code == 422
    assert "name" in response.json()["detail"] or "display_name" in response.json()["detail"]


@pytest.mark.asyncio
async def test_rename_category_atomicity_on_collision(
    seeded_client: AsyncClient,
) -> None:
    """A 422 (collision) leaves the original category unchanged.

    The endpoint raises the 422 *before* the commit, so the
    rollback covers every state change. This test is the
    regression guard for that contract.
    """
    (food_id,) = await _find_category_ids(seeded_client, "Dining Out")
    response = await seeded_client.post(
        f"/api/v1/categories/{food_id}",
        json={"name": "Transportation"},
    )
    assert response.status_code == 422

    # The original Dining Out row is unchanged.
    response = await seeded_client.get("/api/v1/categories")
    assert response.status_code == 200
    by_id = {row["id"]: row for row in response.json()}
    assert by_id[str(food_id)]["name"] == "Dining Out"


# ---------------------------------------------------------------------------
# PATCH /api/v1/transactions/{id} — write-through and deprecation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_patch_transaction_with_category_id_writes_fk_and_label(
    seeded_engine: AsyncEngine, seeded_client: AsyncClient
) -> None:
    """PATCH with ``category_id`` writes the FK + denormalized string + low_confidence=False.

    The endpoint looks up the Category row (404 if missing),
    then writes both the FK and the denormalized label in
    the same commit. The row is marked ``low_confidence=False``
    so the user knows the row is on the closed set.
    """
    tx_id = await _seed_single_transaction(seeded_engine)
    (groceries_id,) = await _find_category_ids(seeded_client, "Groceries")

    response = await seeded_client.patch(
        f"/api/v1/transactions/{tx_id}",
        json={"category_id": str(groceries_id)},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["category_id"] == str(groceries_id)
    assert body["category"] == "Groceries"
    assert body["low_confidence"] is False


@pytest.mark.asyncio
async def test_patch_transaction_with_legacy_category_emits_deprecation_log(
    seeded_engine: AsyncEngine, seeded_client: AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    """PATCH with legacy ``category: str`` sets ``low_confidence=True`` and logs once.

    The endpoint takes the legacy path: clears ``category_id``,
    writes the string, sets ``low_confidence=True``, and
    emits exactly one ``logger.warning`` documenting the
    deprecation. The log fires at most once per request.
    """
    tx_id = await _seed_single_transaction(seeded_engine)

    with caplog.at_level(logging.WARNING, logger="app.api.v1.transactions"):
        response = await seeded_client.patch(
            f"/api/v1/transactions/{tx_id}",
            json={"category": "Custom Label"},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["category_id"] is None
    assert body["category"] == "Custom Label"
    assert body["low_confidence"] is True

    # Exactly one deprecation line, mentioning the field and
    # the migration path.
    deprecation_lines = [
        record.message
        for record in caplog.records
        if "deprecation" in record.message.lower() and "category" in record.message.lower()
    ]
    assert len(deprecation_lines) == 1
    assert "category_id" in deprecation_lines[0]


@pytest.mark.asyncio
async def test_patch_transaction_with_category_id_does_not_emit_deprecation_log(
    seeded_engine: AsyncEngine, seeded_client: AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    """PATCH with ``category_id`` (the preferred path) does NOT emit a deprecation log.

    The deprecation warning is for the legacy ``category: str``
    field only. When the client uses the new contract, the
    log is silent.
    """
    tx_id = await _seed_single_transaction(seeded_engine)
    (groceries_id,) = await _find_category_ids(seeded_client, "Groceries")

    with caplog.at_level(logging.WARNING, logger="app.api.v1.transactions"):
        response = await seeded_client.patch(
            f"/api/v1/transactions/{tx_id}",
            json={"category_id": str(groceries_id)},
        )

    assert response.status_code == 200
    deprecation_lines = [
        record for record in caplog.records if "deprecation" in record.message.lower()
    ]
    assert deprecation_lines == []


@pytest.mark.asyncio
async def test_patch_transaction_404_on_unknown_id(seeded_client: AsyncClient) -> None:
    """PATCH on an unknown transaction UUID returns 404."""
    unknown = uuid.uuid4()
    response = await seeded_client.patch(
        f"/api/v1/transactions/{unknown}",
        json={"category": "X"},
    )
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_patch_transaction_404_on_unknown_category_id(
    seeded_engine: AsyncEngine, seeded_client: AsyncClient
) -> None:
    """PATCH with an unknown ``category_id`` returns 404.

    The category lookup is done before the row is mutated
    so a bad UUID is a clean 404, not a half-applied update.
    """
    tx_id = await _seed_single_transaction(seeded_engine)
    response = await seeded_client.patch(
        f"/api/v1/transactions/{tx_id}",
        json={"category_id": str(uuid.uuid4())},
    )
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_patch_transaction_422_on_empty_body(
    seeded_engine: AsyncEngine, seeded_client: AsyncClient
) -> None:
    """A PATCH body with neither field returns 422."""
    tx_id = await _seed_single_transaction(seeded_engine)
    response = await seeded_client.patch(
        f"/api/v1/transactions/{tx_id}",
        json={},
    )
    assert response.status_code == 422


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _find_category_ids(client: AsyncClient, *names: str) -> tuple[uuid.UUID, ...]:
    """Look up the UUIDs for the given category names via the API.

    Returns a tuple of UUIDs in the same order as ``names``.
    The helper centralises the lookup so the rename / patch
    tests can stay focused on the assertion.
    """
    response = await client.get("/api/v1/categories")
    assert response.status_code == 200
    by_name = {row["name"]: row["id"] for row in response.json()}
    return tuple(uuid.UUID(by_name[name]) for name in names)


async def _seed_single_transaction(engine: AsyncEngine) -> uuid.UUID:
    """Seed a single :class:`Transaction` and return its UUID.

    The transaction is left untagged (no ``category``, no
    ``category_id``) so the PATCH tests can drive both the
    ``category_id`` and the legacy ``category`` paths from
    a clean starting point.
    """
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        bank = Bank(
            name="patch_test_bank",
            display_name="Patch Test Bank",
            password_formula="rut_sin_dv",
        )
        card = CreditCard(
            bank=bank,
            card_number_masked="XXXX XXXX XXXX 7777",
            cardholder="PATCH USER",
            currency="CLP",
        )
        statement = Statement(
            credit_card=card,
            period_start=date(2026, 5, 1),
            period_end=date(2026, 5, 31),
            statement_date=date(2026, 6, 1),
            file_path="patch/test.pdf",
            file_hash="d" * 64,
        )
        tx = Transaction(
            statement=statement,
            date=date(2026, 5, 5),
            description="PATCH-TX",
            amount=Decimal("500.00"),
            currency="CLP",
        )
        session.add_all([bank, card, statement, tx])
        await session.commit()
        return tx.id
