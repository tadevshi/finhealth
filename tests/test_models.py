"""Tests for the Phase 1 domain models.

These tests cover the full Phase 1 entity graph (``Bank`` →
``CreditCard`` → ``Statement`` → ``Transaction``) plus two
correctness properties that are easy to get wrong and expensive to
fix later:

* the ``(credit_card_id, file_hash)`` unique constraint on
  :class:`app.models.statement.Statement` (idempotent uploads);
* the ``Numeric(15, 2)`` precision for :class:`app.models.transaction.Transaction`
  amounts (no float drift).

In-memory SQLite is used for the round-trip tests because it is fast
and fully isolated per engine. The ``file_hash`` uniqueness test
relies on the unique constraint being enforced by SQLite, which it
is — unique constraints are honoured in in-memory mode as well.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import date
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.core.config import Settings, get_settings
from app.db.engine import create_engine
from app.db.session import create_session_factory
from app.models import (
    Bank,
    CreditCard,
    Statement,
    StatementStatus,
    Transaction,
)
from app.models.base import Base


@pytest.fixture
def in_memory_settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    """Settings instance pointing at a fresh in-memory SQLite database.

    Duplicated locally from :mod:`tests.test_db` so this test file
    has no order-of-import dependency. The fixture also clears the
    settings cache so the change is observed by the next
    :func:`get_settings` call.
    """
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
    get_settings.cache_clear()
    return get_settings()


@pytest_asyncio.fixture
async def session_factory(
    in_memory_settings: Settings,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Yield a session factory backed by a fresh in-memory database.

    Exposing the factory (rather than a single session) lets tests
    that need to roll back a failed transaction open a *new* session
    for the next assertion. The original session is poisoned by the
    ``IntegrityError`` and the rollback path is unreliable on the
    async engine (the greenlet context is released by the time
    ``commit()`` raises).
    """
    engine: AsyncEngine = create_engine(in_memory_settings)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        factory = create_session_factory(engine)
        yield factory
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def session(in_memory_settings: Settings) -> AsyncIterator[AsyncSession]:
    """Yield a session backed by a fresh in-memory database with all tables.

    The fixture creates an engine, runs ``create_all`` to materialise
    the full schema, opens a session, and tears everything down at
    the end. ``create_all`` is the right tool here because the tests
    are about ORM behaviour, not migration correctness — Alembic
    round-trips are covered by :mod:`tests.test_alembic`.
    """
    engine: AsyncEngine = create_engine(in_memory_settings)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        factory: async_sessionmaker[AsyncSession] = create_session_factory(engine)
        async with factory() as s:
            yield s
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# Bank
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bank_is_persisted_with_uuid_and_timestamps(session: AsyncSession) -> None:
    """``Bank`` round-trips a row with auto-generated ``id`` and timestamps."""
    bank = Bank(
        name="santander",
        display_name="Banco Santander",
        password_formula="rut_sin_dv",
    )
    session.add(bank)
    await session.commit()
    await session.refresh(bank)

    assert isinstance(bank.id, uuid.UUID)
    assert bank.name == "santander"
    assert bank.display_name == "Banco Santander"
    assert bank.password_formula == "rut_sin_dv"
    assert bank.is_active is True  # default
    assert isinstance(bank.created_at, type(bank.updated_at))
    assert bank.created_at <= bank.updated_at


@pytest.mark.asyncio
async def test_bank_name_must_be_unique(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Two banks with the same ``name`` violate the unique constraint.

    A fresh session is used for the second insert. The original
    session is left untouched, sidestepping the greenlet-context
    issue that arises when ``commit()`` raises ``IntegrityError``
    on the async engine. See
    :func:`test_file_hash_unique_per_credit_card` for the full
    reasoning.
    """
    async with session_factory() as s:
        s.add(
            Bank(name="itau", display_name="Itaú", password_formula="rut_sin_dv"),
        )
        await s.commit()

    async with session_factory() as s:
        s.add(
            Bank(
                name="itau",
                display_name="Itaú Different",
                password_formula="rut_sin_dv",
            ),
        )
        with pytest.raises(IntegrityError):
            await s.commit()


@pytest.mark.asyncio
async def test_bank_can_be_queried_by_name(session: AsyncSession) -> None:
    """A bank is retrievable by its short name after persistence."""
    session.add(
        Bank(
            name="banco_de_chile",
            display_name="Banco de Chile",
            password_formula="rut_ultimos_4",
        ),
    )
    await session.commit()

    stmt = select(Bank).where(Bank.name == "banco_de_chile")
    result = (await session.execute(stmt)).scalar_one()
    assert result.display_name == "Banco de Chile"


# ---------------------------------------------------------------------------
# Bank ↔ CreditCard
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_credit_card_belongs_to_a_bank(session: AsyncSession) -> None:
    """``CreditCard.bank`` resolves to the parent :class:`Bank` row."""
    bank = Bank(
        name="santander",
        display_name="Banco Santander",
        password_formula="rut_sin_dv",
    )
    card = CreditCard(
        bank=bank,
        card_number_masked="XXXX XXXX XXXX 0951",
        cardholder="JOHN DOE",
        currency="CLP",
    )
    session.add_all([bank, card])
    await session.commit()
    await session.refresh(card)

    assert isinstance(card.id, uuid.UUID)
    assert card.bank_id == bank.id
    assert card.bank.name == "santander"
    assert card.bank.display_name == "Banco Santander"
    assert card.is_active is True


@pytest.mark.asyncio
async def test_bank_relationship_lists_cards(session: AsyncSession) -> None:
    """``Bank.credit_cards`` exposes all cards that reference the bank."""
    bank = Bank(
        name="itau",
        display_name="Itaú",
        password_formula="rut_sin_dv",
    )
    session.add(bank)
    await session.flush()

    card_a = CreditCard(
        bank_id=bank.id,
        card_number_masked="XXXX XXXX XXXX 1111",
        cardholder="ALICE",
        currency="CLP",
    )
    card_b = CreditCard(
        bank_id=bank.id,
        card_number_masked="XXXX XXXX XXXX 2222",
        cardholder="BOB",
        currency="USD",
    )
    session.add_all([card_a, card_b])
    await session.commit()

    # ``selectin`` loading populates the relationship on access.
    await session.refresh(bank, attribute_names=["credit_cards"])
    card_masked = {c.card_number_masked for c in bank.credit_cards}
    assert card_masked == {"XXXX XXXX XXXX 1111", "XXXX XXXX XXXX 2222"}


# ---------------------------------------------------------------------------
# CreditCard ↔ Statement
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_statement_belongs_to_a_credit_card(session: AsyncSession) -> None:
    """``Statement.credit_card`` resolves to the parent card."""
    bank = Bank(name="santander", display_name="Banco Santander", password_formula="rut_sin_dv")
    card = CreditCard(
        bank=bank,
        card_number_masked="XXXX XXXX XXXX 0951",
        cardholder="JOHN DOE",
        currency="CLP",
    )
    statement = Statement(
        credit_card=card,
        period_start=date(2026, 5, 1),
        period_end=date(2026, 5, 31),
        statement_date=date(2026, 6, 1),
        file_path="santander/2026-05.pdf",
        file_hash="a" * 64,
    )
    session.add_all([bank, card, statement])
    await session.commit()
    await session.refresh(statement)

    assert statement.credit_card_id == card.id
    assert statement.credit_card.card_number_masked == "XXXX XXXX XXXX 0951"
    assert statement.status is StatementStatus.PENDING  # default


@pytest.mark.asyncio
async def test_file_hash_unique_per_credit_card(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Re-uploading the same file for the same card violates the constraint.

    The ``(credit_card_id, file_hash)`` unique constraint is the
    ingestion pipeline's idempotency guard. Two statements on the
    *same* card with the *same* hash must fail; the same hash on a
    *different* card is fine.

    The test uses a *new* session for the failing insert and the
    follow-up assertion. The previous session is poisoned by the
    ``IntegrityError`` and calling ``session.rollback()`` on the
    async engine from the same call site is unreliable — the
    greenlet context is released by the time ``commit()`` raises.
    Opening a fresh session keeps the rest of the test on a clean
    slate.
    """
    # Seed a bank and two cards using the first session.
    async with session_factory() as s:
        bank = Bank(name="itau", display_name="Itaú", password_formula="rut_sin_dv")
        card = CreditCard(
            bank=bank,
            card_number_masked="XXXX XXXX XXXX 0951",
            cardholder="JOHN DOE",
            currency="CLP",
        )
        other_card = CreditCard(
            bank=bank,
            card_number_masked="XXXX XXXX XXXX 2222",
            cardholder="ALICE",
            currency="CLP",
        )
        s.add_all([bank, card, other_card])
        await s.commit()

    shared_hash = "f" * 64

    # First statement on ``card`` — must succeed.
    async with session_factory() as s:
        card = (await s.execute(select(CreditCard).where(CreditCard.id == card.id))).scalar_one()
        s.add(
            Statement(
                credit_card_id=card.id,
                period_start=date(2026, 5, 1),
                period_end=date(2026, 5, 31),
                statement_date=date(2026, 6, 1),
                file_path="itau/card1/2026-05.pdf",
                file_hash=shared_hash,
            ),
        )
        await s.commit()

    # Second statement on the same card with the same hash — must fail.
    async with session_factory() as s:
        card = (await s.execute(select(CreditCard).where(CreditCard.id == card.id))).scalar_one()
        s.add(
            Statement(
                credit_card_id=card.id,
                period_start=date(2026, 4, 1),
                period_end=date(2026, 4, 30),
                statement_date=date(2026, 5, 1),
                file_path="itau/card1/2026-04.pdf",
                file_hash=shared_hash,
            ),
        )
        with pytest.raises(IntegrityError):
            await s.commit()

    # Same hash on a *different* card — must succeed.
    async with session_factory() as s:
        other_card = (
            await s.execute(select(CreditCard).where(CreditCard.id == other_card.id))
        ).scalar_one()
        s.add(
            Statement(
                credit_card_id=other_card.id,
                period_start=date(2026, 5, 1),
                period_end=date(2026, 5, 31),
                statement_date=date(2026, 6, 1),
                file_path="itau/card2/2026-05.pdf",
                file_hash=shared_hash,
            ),
        )
        await s.commit()


# ---------------------------------------------------------------------------
# Statement ↔ Transaction
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_transactions_belong_to_a_statement(session: AsyncSession) -> None:
    """``Transaction.statement`` resolves to the parent :class:`Statement`."""
    bank = Bank(name="santander", display_name="Banco Santander", password_formula="rut_sin_dv")
    card = CreditCard(
        bank=bank,
        card_number_masked="XXXX XXXX XXXX 0951",
        cardholder="JOHN DOE",
        currency="CLP",
    )
    statement = Statement(
        credit_card=card,
        period_start=date(2026, 5, 1),
        period_end=date(2026, 5, 31),
        statement_date=date(2026, 6, 1),
        file_path="santander/2026-05.pdf",
        file_hash="b" * 64,
    )
    tx = Transaction(
        statement=statement,
        date=date(2026, 5, 5),
        description="COFFEE SHOP",
        amount=Decimal("4250.00"),
        currency="CLP",
    )
    session.add_all([bank, card, statement, tx])
    await session.commit()
    await session.refresh(tx)

    assert tx.statement_id == statement.id
    assert tx.statement.file_hash == "b" * 64
    assert tx.category is None  # default
    assert tx.installment_number is None
    assert tx.installment_value is None


@pytest.mark.asyncio
async def test_transaction_amount_preserves_decimal_precision(session: AsyncSession) -> None:
    """``Transaction.amount`` round-trips as :class:`Decimal`, no float drift.

    ``0.10`` cannot be represented exactly in binary floating point.
    If the column were a ``Float`` the round-trip would yield
    ``0.1000000000000000055...``. The ``Numeric(15, 2)`` column
    preserves the canonical decimal form, which is the whole point
    of the constraint.
    """
    bank = Bank(name="santander", display_name="Banco Santander", password_formula="rut_sin_dv")
    card = CreditCard(
        bank=bank,
        card_number_masked="XXXX XXXX XXXX 0951",
        cardholder="JOHN DOE",
        currency="CLP",
    )
    statement = Statement(
        credit_card=card,
        period_start=date(2026, 5, 1),
        period_end=date(2026, 5, 31),
        statement_date=date(2026, 6, 1),
        file_path="santander/2026-05.pdf",
        file_hash="c" * 64,
    )
    precise = Decimal("0.10")
    tx = Transaction(
        statement=statement,
        date=date(2026, 5, 5),
        description="PARKING",
        amount=precise,
        currency="CLP",
    )
    session.add_all([bank, card, statement, tx])
    await session.commit()
    await session.refresh(tx)

    assert isinstance(tx.amount, Decimal)
    # The string form is the round-tripped canonical form: no
    # float-induced junk digits.
    assert str(tx.amount) == "0.10"
    assert tx.amount == precise


@pytest.mark.asyncio
async def test_transaction_installment_fields_are_optional(session: AsyncSession) -> None:
    """Installment fields are ``None`` for one-off charges and populated otherwise."""
    bank = Bank(name="santander", display_name="Banco Santander", password_formula="rut_sin_dv")
    card = CreditCard(
        bank=bank,
        card_number_masked="XXXX XXXX XXXX 0951",
        cardholder="JOHN DOE",
        currency="CLP",
    )
    statement = Statement(
        credit_card=card,
        period_start=date(2026, 5, 1),
        period_end=date(2026, 5, 31),
        statement_date=date(2026, 6, 1),
        file_path="santander/2026-05.pdf",
        file_hash="d" * 64,
    )
    one_off = Transaction(
        statement=statement,
        date=date(2026, 5, 5),
        description="GROCERIES",
        amount=Decimal("12345.67"),
        currency="CLP",
    )
    installment = Transaction(
        statement=statement,
        date=date(2026, 5, 5),
        description="TV 3/12",
        amount=Decimal("49990.00"),
        currency="CLP",
        installment_number=3,
        installment_total=12,
        installment_value=Decimal("49990.00"),
    )
    session.add_all([bank, card, statement, one_off, installment])
    await session.commit()

    assert one_off.installment_number is None
    assert one_off.installment_total is None
    assert one_off.installment_value is None

    assert installment.installment_number == 3
    assert installment.installment_total == 12
    assert installment.installment_value == Decimal("49990.00")


@pytest.mark.asyncio
async def test_transaction_raw_json_round_trips_verbatim(session: AsyncSession) -> None:
    """``raw_json`` preserves the original LLM extraction output as JSON."""
    bank = Bank(name="santander", display_name="Banco Santander", password_formula="rut_sin_dv")
    card = CreditCard(
        bank=bank,
        card_number_masked="XXXX XXXX XXXX 0951",
        cardholder="JOHN DOE",
        currency="CLP",
    )
    statement = Statement(
        credit_card=card,
        period_start=date(2026, 5, 1),
        period_end=date(2026, 5, 31),
        statement_date=date(2026, 6, 1),
        file_path="santander/2026-05.pdf",
        file_hash="e" * 64,
    )
    raw = {
        "fecha": "2026-05-05",
        "descripcion": "COFFEE SHOP",
        "monto": 4250,
        "moneda": "CLP",
        "confidence": 0.97,
    }
    tx = Transaction(
        statement=statement,
        date=date(2026, 5, 5),
        description="COFFEE SHOP",
        amount=Decimal("4250.00"),
        currency="CLP",
        raw_json=raw,
    )
    session.add_all([bank, card, statement, tx])
    await session.commit()
    await session.refresh(tx)

    assert tx.raw_json == raw
