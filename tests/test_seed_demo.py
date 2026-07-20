"""Demo seed hardening tests."""

import json
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import func, inspect, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.cli import seed_demo
from app.models import Bank, Category, CreditCard, Merchant, RecurringRule, Statement, Transaction
from app.models.base import Base
from app.models.statement import StatementStatus
from app.services.dashboard import DashboardService


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("dining", "Dining Out"),
        (" DÍNING ", "Dining Out"),
        ("services", "Bills"),
        ("something_new", "Uncategorized"),
    ],
)
def test_category_alias_normalization(raw: str, expected: str) -> None:
    """Threat matrix: aliases are case/Unicode normalized and unknown maps to Uncategorized."""
    assert seed_demo.resolve_category_name(raw) == expected


def _contains_seed_marker(value: object) -> bool:
    if isinstance(value, dict):
        return value.get("seed_provenance") == seed_demo.SEED_PROVENANCE
    return isinstance(value, str) and seed_demo.SEED_PROVENANCE in value


@pytest.mark.asyncio
async def test_every_seed_row_carries_provenance_marker(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Threat matrix: every seed-created row type carries stable provenance."""
    db_path = tmp_path / "seed-provenance.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_path}")
    seed_demo.get_settings.cache_clear()

    await seed_demo.seed_demo()

    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    session_maker = async_sessionmaker(engine, expire_on_commit=False)
    async with session_maker() as session:
        banks = (await session.execute(select(Bank))).scalars().all()
        cards = (await session.execute(select(CreditCard))).scalars().all()
        merchants = (await session.execute(select(Merchant))).scalars().all()
        categories = (await session.execute(select(Category))).scalars().all()
        statements = (await session.execute(select(Statement))).scalars().all()
        transactions = (await session.execute(select(Transaction))).scalars().all()
        recurring = (await session.execute(select(RecurringRule))).scalars().all()

    await engine.dispose()

    assert banks and all(_contains_seed_marker(row.display_name) for row in banks)
    assert cards and all(_contains_seed_marker(row.cardholder) for row in cards)
    assert merchants and all(_contains_seed_marker(row.name) for row in merchants)
    assert categories and all(_contains_seed_marker(row.display_name) for row in categories)
    assert statements and all(_contains_seed_marker(row.error_message) for row in statements)
    assert transactions and all(_contains_seed_marker(row.raw_json) for row in transactions)
    assert recurring and all(_contains_seed_marker(row.period_label) for row in recurring)

    assert {row.id for row in banks} == {seed_demo._seed_uuid(f"bank/{seed_demo.BANK_NAME}")}
    assert {row.id for row in cards} == {
        seed_demo._seed_uuid(f"card/{seed_demo.CARD_CLP_MASK}"),
        seed_demo._seed_uuid(f"card/{seed_demo.CARD_USD_MASK}"),
    }
    assert {row.id for row in merchants} == {
        seed_demo._seed_uuid(f"merchant/{slug}")
        for slug in sorted(
            {m for _, _, m, _, _, _ in seed_demo.TX_PLAN}
            | {m for m, *_ in seed_demo.RECURRING_RULES}
        )
    }
    assert {row.id for row in recurring} == {
        seed_demo._seed_uuid(f"recurring/{row.merchant_id}/{row.currency}/{row.period_days}")
        for row in recurring
    }


@pytest.mark.asyncio
async def test_user_row_marker_unchanged_after_seed(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Threat matrix: seed provenance is never written onto a user-owned transaction."""
    db_path = tmp_path / "seed-user-preservation.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_path}")
    seed_demo.get_settings.cache_clear()

    user_tx_id, before = await _insert_user_transaction(db_path, raw_json=None)

    await seed_demo.seed_demo()

    after = await _transaction_values(db_path, user_tx_id)
    assert after == before


@pytest.mark.asyncio
async def test_seed_is_repeat_safe_and_preserves_user_rows(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Threat matrix: two runs are identical, user rows survive, seed never attaches to user statements."""
    db_path = tmp_path / "seed.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_path}")
    seed_demo.get_settings.cache_clear()

    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    session_maker = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with session_maker() as session:
        for idx, name in enumerate(
            [
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
            ],
            start=1,
        ):
            session.add(Category(name=name, display_name=name, sort_order=idx))
        bank = Bank(
            name="user_bank", display_name="User Bank", password_formula="rut", is_active=True
        )
        session.add(bank)
        await session.commit()
        await session.refresh(bank)
        card = CreditCard(
            bank_id=bank.id,
            card_number_masked="XXXX XXXX XXXX 9999",
            cardholder="USER",
            currency="CLP",
            is_active=True,
        )
        session.add(card)
        await session.commit()
        await session.refresh(card)
        user_statement = Statement(
            credit_card_id=card.id,
            period_start=date(2026, 7, 1),
            period_end=date(2026, 7, 31),
            statement_date=date(2026, 7, 31),
            file_path="/tmp/user.pdf",
            file_hash="user-hash",
            status=StatementStatus.COMPLETED,
        )
        session.add(user_statement)
        await session.commit()
        await session.refresh(user_statement)
        user_tx = Transaction(
            statement_id=user_statement.id,
            date=date(2026, 7, 7),
            description="USER TX",
            amount=Decimal("123.00"),
            currency="CLP",
            raw_json=None,
        )
        session.add(user_tx)
        await session.commit()
        user_statement_id = user_statement.id
        user_tx_id = user_tx.id

    await engine.dispose()

    await seed_demo.seed_demo()
    first = await _snapshot(db_path)
    await seed_demo.seed_demo()
    second = await _snapshot(db_path)

    assert first == second

    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    session_maker = async_sessionmaker(engine, expire_on_commit=False)
    async with session_maker() as session:
        user_row = await session.get(Transaction, user_tx_id)
        assert user_row is not None
        assert user_row.raw_json is None
        seed_on_user_statement = await session.scalar(
            select(func.count(Transaction.id)).where(
                Transaction.statement_id == user_statement_id,
                Transaction.raw_json["seed_provenance"].as_string() == seed_demo.SEED_PROVENANCE,
            )
        )
        assert seed_on_user_statement == 0
    await engine.dispose()


@pytest.mark.asyncio
async def test_two_runs_seed_owned_row_snapshot_stable(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """All seed-owned row types are byte-stable across reruns; user rows stay untouched."""
    db_path = tmp_path / "seed-owned-snapshot.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_path}")
    seed_demo.get_settings.cache_clear()

    await seed_demo.seed_demo()
    first = await _seed_owned_snapshot(db_path)
    await seed_demo.seed_demo()
    second = await _seed_owned_snapshot(db_path)

    assert first == second
    assert set(first) == {
        "banks",
        "credit_cards",
        "merchants",
        "categories",
        "statements",
        "transactions",
        "recurring_rules",
    }
    assert all(first[row_type] for row_type in first)


@pytest.mark.asyncio
async def test_two_runs_full_mapped_column_snapshot(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every seed-owned non-timestamp mapped field is stable across reruns."""
    db_path = tmp_path / "seed-owned-full-snapshot.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_path}")
    seed_demo.get_settings.cache_clear()

    await seed_demo.seed_demo()
    first = await _seed_owned_snapshot(db_path)
    await seed_demo.seed_demo()
    second = await _seed_owned_snapshot(db_path)

    assert first == second
    assert "installment_number" in first.transactions[0]
    assert "installment_total" in first.transactions[0]
    assert "installment_value" in first.transactions[0]
    assert "recurring_rule_id" in first.transactions[0]
    assert "low_confidence" in first.transactions[0]
    assert "amount_min" in first.recurring_rules[0]
    assert "amount_max" in first.recurring_rules[0]
    assert "confidence" in first.recurring_rules[0]
    assert "occurrences" in first.recurring_rules[0]
    assert "last_seen_date" in first.recurring_rules[0]
    assert "is_active" in first.recurring_rules[0]


@pytest.mark.asyncio
async def test_unknown_alias_routes_to_uncategorized_via_seed(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A test-local unknown seed alias creates Uncategorized spend without touching users."""
    db_path = tmp_path / "seed-unknown-alias.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_path}")
    seed_demo.get_settings.cache_clear()
    sentinel = (2026, 7, "jumbo", "completely_new_alias_xyz", 1, "CLP")
    monkeypatch.setattr(seed_demo, "TX_PLAN", [*seed_demo.TX_PLAN, sentinel])

    await seed_demo.seed_demo()

    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    session_maker = async_sessionmaker(engine, expire_on_commit=False)
    async with session_maker() as session:
        uncategorized = await session.scalar(select(Category).where(Category.name == "Uncategorized"))
        assert uncategorized is not None
        assert _contains_seed_marker(uncategorized.display_name)
        row = await session.scalar(
            select(Transaction).where(
                Transaction.raw_json["category_key"].as_string() == "completely_new_alias_xyz"
            )
        )
        assert row is not None
        assert row.category == "Uncategorized"
        assert row.category_id == uncategorized.id
        assert row.raw_json["canonical_category"] == "Uncategorized"
    await engine.dispose()


@pytest.mark.asyncio
async def test_unknown_alias_seeds_two_uncategorized_dashboard_rows(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unknown aliases flow through seed_demo and aggregate under Uncategorized."""
    db_path = tmp_path / "seed-unknown-alias-dashboard.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_path}")
    seed_demo.get_settings.cache_clear()
    sentinels = [
        (2026, 7, "jumbo", "completely_new_alias_a", 100, "CLP"),
        (2026, 7, "lider", "completely_new_alias_b", 200, "CLP"),
    ]
    monkeypatch.setattr(seed_demo, "TX_PLAN", [*seed_demo.TX_PLAN, *sentinels])

    await seed_demo.seed_demo()

    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    session_maker = async_sessionmaker(engine, expire_on_commit=False)
    async with session_maker() as session:
        uncategorized = await session.scalar(select(Category).where(Category.name == "Uncategorized"))
        assert uncategorized is not None
        assert _contains_seed_marker(uncategorized.display_name)
        rows = (
            await session.execute(
                select(Transaction).where(
                    Transaction.raw_json["category_key"].as_string().in_(
                        ["completely_new_alias_a", "completely_new_alias_b"]
                    )
                )
            )
        ).scalars().all()
        assert len(rows) == 2
        assert {row.category for row in rows} == {"Uncategorized"}
        assert {row.category_id for row in rows} == {uncategorized.id}

        dashboard_rows = await DashboardService(session).categories(period=date(2026, 7, 1))

    await engine.dispose()
    uncategorized_row = next(row for row in dashboard_rows if row.category_id == uncategorized.id)
    assert uncategorized_row.total_per_currency == {"CLP": Decimal("300.00")}
    assert uncategorized_row.transaction_count == 2


@pytest.mark.asyncio
async def test_exact_id_collision_rejected_for_every_seed_entity(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unmarked exact deterministic IDs are rejected before seed-owned mutation."""
    entity_cases = [
        (Bank, f"bank/{seed_demo.BANK_NAME}", {"name": seed_demo.BANK_NAME, "display_name": "User Bank", "password_formula": "rut", "is_active": True}, "display_name"),
        (CreditCard, f"card/{seed_demo.CARD_CLP_MASK}", {"card_number_masked": seed_demo.CARD_CLP_MASK, "cardholder": "USER CARD", "currency": "CLP", "is_active": True}, "cardholder"),
        (Merchant, "merchant/jumbo", {"name": "user_jumbo", "is_active": True}, "name"),
        (Category, "category/Dining Out", {"name": "Dining Out", "display_name": "User Dining", "sort_order": 1}, "display_name"),
        (RecurringRule, None, {"period_days": 30, "period_label": "user-monthly", "amount_min": Decimal("11900.00"), "amount_max": Decimal("11900.00"), "currency": "CLP", "confidence": 0.5, "occurrences": 1, "last_seen_date": date(2026, 7, 1), "is_active": True}, "period_label"),
    ]
    for model, stable_key, values, marker_field in entity_cases:
        db_path = tmp_path / f"collision-{model.__name__.lower()}.db"
        monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_path}")
        seed_demo.get_settings.cache_clear()
        await seed_demo.seed_demo()

        engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
        session_maker = async_sessionmaker(engine, expire_on_commit=False)
        async with session_maker() as session:
            if model is RecurringRule:
                merchant = await session.scalar(select(Merchant).where(Merchant.name.like("%netflix%")))
                assert merchant is not None
                stable_key = f"recurring/{merchant.id}/CLP/30"
                values = {**values, "merchant_id": merchant.id}
            assert stable_key is not None
            collision_id = seed_demo._seed_uuid(stable_key)
            row = await session.get(model, collision_id)
            assert row is not None
            for field, value in values.items():
                setattr(row, field, value)
            await session.commit()
            before = await _row_snapshot(session, model, collision_id)
        await engine.dispose()

        with pytest.raises(RuntimeError, match=r"seed .* collision"):
            await seed_demo.seed_demo()

        engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
        session_maker = async_sessionmaker(engine, expire_on_commit=False)
        async with session_maker() as session:
            after = await _row_snapshot(session, model, collision_id)
            count = await session.scalar(select(func.count(model.id)).where(model.id == collision_id))
            row = await session.get(model, collision_id)
            assert row is not None
            assert not _contains_seed_marker(getattr(row, marker_field))
        await engine.dispose()
        assert after == before
        assert count == 1


@pytest.mark.asyncio
async def test_exact_file_hash_collision_rejected_for_statement(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unmarked exact statement file_hash collision is rejected without mutation."""
    db_path = tmp_path / "statement-hash-collision.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_path}")
    seed_demo.get_settings.cache_clear()
    await seed_demo.seed_demo()

    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    session_maker = async_sessionmaker(engine, expire_on_commit=False)
    async with session_maker() as session:
        card = await session.get(CreditCard, seed_demo._seed_uuid(f"card/{seed_demo.CARD_CLP_MASK}"))
        assert card is not None
        seed_hash = seed_demo._statement_hash(str(card.id), 2026, 7)
        statement = await session.scalar(select(Statement).where(Statement.file_hash == seed_hash))
        assert statement is not None
        statement.error_message = "user-owned statement"
        statement.file_path = "/tmp/user-owned-statement.pdf"
        for tx in (
            await session.execute(select(Transaction).where(Transaction.statement_id == statement.id))
        ).scalars().all():
            await session.delete(tx)
        await session.commit()
        before = await _row_snapshot(session, Statement, statement.id)
        statement_id = statement.id
    await engine.dispose()

    with pytest.raises(RuntimeError, match="seed statement collision"):
        await seed_demo.seed_demo()

    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    session_maker = async_sessionmaker(engine, expire_on_commit=False)
    async with session_maker() as session:
        after = await _row_snapshot(session, Statement, statement_id)
        seed_tx_on_user_statement = await session.scalar(
            select(func.count(Transaction.id)).where(
                Transaction.statement_id == statement_id,
                Transaction.raw_json["seed_provenance"].as_string() == seed_demo.SEED_PROVENANCE,
            )
        )
    await engine.dispose()
    assert after == before
    assert seed_tx_on_user_statement == 0


@pytest.mark.asyncio
async def test_seed_collision_user_statement_same_card_period(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Threat matrix: seed statements reconcile by seed hash, not card+period."""
    db_path = tmp_path / "seed-statement-collision.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_path}")
    seed_demo.get_settings.cache_clear()

    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    session_maker = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with session_maker() as session:
        bank = Bank(
            id=seed_demo._seed_uuid(f"bank/{seed_demo.BANK_NAME}"),
            name=seed_demo.BANK_NAME,
            display_name=seed_demo._marked_text(
                seed_demo.BANK_DISPLAY,
                f"bank/{seed_demo.BANK_NAME}",
            ),
            password_formula="rut",
            is_active=True,
        )
        session.add(bank)
        await session.commit()
        card = CreditCard(
            id=seed_demo._seed_uuid(f"card/{seed_demo.CARD_CLP_MASK}"),
            bank_id=bank.id,
            card_number_masked=seed_demo.CARD_CLP_MASK,
            cardholder=seed_demo._marked_text("USER CARD", f"card/{seed_demo.CARD_CLP_MASK}"),
            currency="CLP",
            is_active=True,
        )
        session.add(card)
        await session.commit()
        user_statement = Statement(
            credit_card_id=card.id,
            period_start=date(2026, 7, 1),
            period_end=date(2026, 7, 31),
            statement_date=date(2026, 7, 31),
            file_path="/tmp/user-same-card.pdf",
            file_hash="user-same-card-hash",
            status=StatementStatus.COMPLETED,
        )
        session.add(user_statement)
        await session.commit()
        await session.refresh(user_statement)
        user_tx = Transaction(
            statement_id=user_statement.id,
            date=date(2026, 7, 9),
            description="USER SAME CARD TX",
            amount=Decimal("42.00"),
            currency="CLP",
            raw_json=None,
        )
        session.add(user_tx)
        await session.commit()
        user_statement_id = user_statement.id
        user_statement_before = (
            user_statement.file_hash,
            user_statement.error_message,
            user_statement.file_path,
        )
    await engine.dispose()

    await seed_demo.seed_demo()

    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    session_maker = async_sessionmaker(engine, expire_on_commit=False)
    async with session_maker() as session:
        user_statement_after = await session.get(Statement, user_statement_id)
        assert user_statement_after is not None
        assert (
            user_statement_after.file_hash,
            user_statement_after.error_message,
            user_statement_after.file_path,
        ) == user_statement_before
        seed_hash = seed_demo._statement_hash(str(card.id), 2026, 7)
        seed_statement = await session.scalar(select(Statement).where(Statement.file_hash == seed_hash))
        assert seed_statement is not None
        assert seed_statement.id != user_statement_id
        seed_on_user_statement = await session.scalar(
            select(func.count(Transaction.id)).where(
                Transaction.statement_id == user_statement_id,
                Transaction.raw_json["seed_provenance"].as_string() == seed_demo.SEED_PROVENANCE,
            )
        )
        assert seed_on_user_statement == 0
    await engine.dispose()


@pytest.mark.asyncio
async def test_two_runs_user_value_equality(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Threat matrix: user transaction values stay byte-identical across seed reruns."""
    db_path = tmp_path / "seed-user-value-equality.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_path}")
    seed_demo.get_settings.cache_clear()

    user_tx_id, before = await _insert_user_transaction(db_path, raw_json={"note": "keep"})

    await seed_demo.seed_demo()
    await seed_demo.seed_demo()

    after = await _transaction_values(db_path, user_tx_id)
    assert after == before


@pytest.mark.asyncio
async def test_two_runs_user_full_value_equality(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every non-timestamp mapped user Transaction value survives seed reruns."""
    db_path = tmp_path / "seed-user-full-value-equality.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_path}")
    seed_demo.get_settings.cache_clear()
    await seed_demo.seed_demo()

    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    session_maker = async_sessionmaker(engine, expire_on_commit=False)
    async with session_maker() as session:
        statement = await session.scalar(select(Statement).order_by(Statement.period_end.desc()))
        merchant = await session.scalar(select(Merchant).where(Merchant.name.like("%jumbo%")))
        category = await session.scalar(select(Category).where(Category.name == "Dining Out"))
        recurring = await session.scalar(select(RecurringRule).order_by(RecurringRule.id))
        assert statement is not None
        assert merchant is not None
        assert category is not None
        assert recurring is not None
        user_tx = Transaction(
            statement_id=statement.id,
            merchant_id=merchant.id,
            category_id=category.id,
            category="Dining Out",
            amount=Decimal("1234.00"),
            currency="CLP",
            date=date(2026, 7, 23),
            description="USER INSTALLMENT TX",
            raw_json={"note": "keep"},
            installment_number=2,
            installment_total=12,
            installment_value=Decimal("5000.00"),
            recurring_rule_id=recurring.id,
            low_confidence=True,
        )
        session.add(user_tx)
        await session.commit()
        user_tx_id = user_tx.id
        before = await _row_snapshot(session, Transaction, user_tx_id)
    await engine.dispose()

    await seed_demo.seed_demo()
    await seed_demo.seed_demo()

    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    session_maker = async_sessionmaker(engine, expire_on_commit=False)
    async with session_maker() as session:
        after = await _row_snapshot(session, Transaction, user_tx_id)
    await engine.dispose()
    assert after == before


async def _insert_user_transaction(
    db_path,
    *,
    raw_json: dict[str, str] | None,
) -> tuple[UUID, tuple[object, ...]]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    session_maker = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with session_maker() as session:
        bank = Bank(
            name="user_bank", display_name="User Bank", password_formula="rut", is_active=True
        )
        session.add(bank)
        await session.commit()
        await session.refresh(bank)
        card = CreditCard(
            bank_id=bank.id,
            card_number_masked="XXXX XXXX XXXX 9999",
            cardholder="USER",
            currency="CLP",
            is_active=True,
        )
        session.add(card)
        await session.commit()
        await session.refresh(card)
        user_statement = Statement(
            credit_card_id=card.id,
            period_start=date(2026, 7, 1),
            period_end=date(2026, 7, 31),
            statement_date=date(2026, 7, 31),
            file_path="/tmp/user.pdf",
            file_hash="user-hash",
            status=StatementStatus.COMPLETED,
        )
        session.add(user_statement)
        await session.commit()
        await session.refresh(user_statement)
        user_tx = Transaction(
            statement_id=user_statement.id,
            date=date(2026, 7, 7),
            description="USER TX",
            amount=Decimal("123.00"),
            currency="CLP",
            raw_json=raw_json,
        )
        session.add(user_tx)
        await session.commit()
        user_tx_id = user_tx.id
    before = await _transaction_values(db_path, user_tx_id)
    await engine.dispose()
    return user_tx_id, before


async def _transaction_values(db_path, tx_id: UUID) -> tuple[object, ...]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    session_maker = async_sessionmaker(engine, expire_on_commit=False)
    async with session_maker() as session:
        row = await session.get(Transaction, tx_id)
        assert row is not None
        values = tuple(_serialize_value(getattr(row, column)) for column in _mapped_columns(Transaction))
    await engine.dispose()
    return values


async def _snapshot(db_path) -> list[tuple[str, str, str, str, str]]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    session_maker = async_sessionmaker(engine, expire_on_commit=False)
    async with session_maker() as session:
        rows = (
            await session.execute(
                select(
                    Transaction.id,
                    Transaction.statement_id,
                    Transaction.date,
                    Transaction.description,
                    Transaction.amount,
                )
                .where(
                    Transaction.raw_json["seed_provenance"].as_string() == seed_demo.SEED_PROVENANCE
                )
                .order_by(Transaction.id)
            )
        ).all()
    await engine.dispose()
    return [
        (str(r.id), str(r.statement_id), r.date.isoformat(), r.description, str(r.amount))
        for r in rows
    ]


@dataclass(frozen=True, slots=True)
class SeedOwnedSnapshot:
    banks: list[dict[str, object]]
    credit_cards: list[dict[str, object]]
    merchants: list[dict[str, object]]
    categories: list[dict[str, object]]
    statements: list[dict[str, object]]
    transactions: list[dict[str, object]]
    recurring_rules: list[dict[str, object]]

    def __iter__(self):
        return iter(self.__dataclass_fields__)  # type: ignore[attr-defined]

    def __getitem__(self, key: str) -> list[dict[str, object]]:
        return getattr(self, key)


def _serialize_value(value: object) -> object:
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, date):
        return value.isoformat()
    if hasattr(value, "value"):
        return value.value  # type: ignore[no-any-return]
    if isinstance(value, (dict, list)):
        return json.dumps(value, sort_keys=True, default=str)
    return value


def _mapped_columns(model: type[object]) -> list[str]:
    return [
        column.key
        for column in inspect(model).columns
        if column.key not in {"created_at", "updated_at"}
    ]


async def _row_snapshot(session, model: type[Any], row_id: UUID) -> dict[str, object]:
    row = await session.get(model, row_id)
    assert row is not None
    return {column: _serialize_value(getattr(row, column)) for column in _mapped_columns(model)}


async def _seed_owned_snapshot(db_path) -> SeedOwnedSnapshot:
    """Return deterministic non-timestamp snapshots for every seed-owned row type."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    session_maker = async_sessionmaker(engine, expire_on_commit=False)
    async with session_maker() as session:
        banks = (await session.execute(select(Bank).order_by(Bank.id))).scalars().all()
        cards = (await session.execute(select(CreditCard).order_by(CreditCard.id))).scalars().all()
        merchants = (await session.execute(select(Merchant).order_by(Merchant.id))).scalars().all()
        categories = (await session.execute(select(Category).order_by(Category.id))).scalars().all()
        statements = (await session.execute(select(Statement).order_by(Statement.id))).scalars().all()
        transactions = (await session.execute(select(Transaction).order_by(Transaction.id))).scalars().all()
        recurring = (await session.execute(select(RecurringRule).order_by(RecurringRule.id))).scalars().all()
    await engine.dispose()

    return SeedOwnedSnapshot(
        banks=[
            {column: _serialize_value(getattr(row, column)) for column in _mapped_columns(Bank)}
            for row in banks
            if _contains_seed_marker(row.display_name)
        ],
        credit_cards=[
            {column: _serialize_value(getattr(row, column)) for column in _mapped_columns(CreditCard)}
            for row in cards
            if _contains_seed_marker(row.cardholder)
        ],
        merchants=[
            {column: _serialize_value(getattr(row, column)) for column in _mapped_columns(Merchant)}
            for row in merchants
            if _contains_seed_marker(row.name)
        ],
        categories=[
            {column: _serialize_value(getattr(row, column)) for column in _mapped_columns(Category)}
            for row in categories
            if _contains_seed_marker(row.display_name)
        ],
        statements=[
            {column: _serialize_value(getattr(row, column)) for column in _mapped_columns(Statement)}
            for row in statements
            if _contains_seed_marker(row.error_message)
        ],
        transactions=[
            {column: _serialize_value(getattr(row, column)) for column in _mapped_columns(Transaction)}
            for row in transactions
            if _contains_seed_marker(row.raw_json)
        ],
        recurring_rules=[
            {column: _serialize_value(getattr(row, column)) for column in _mapped_columns(RecurringRule)}
            for row in recurring
            if _contains_seed_marker(row.period_label)
        ],
    )
