"""Tests for Alembic migration plumbing.

These tests exercise the full upgrade/downgrade round-trip against a
temporary SQLite file and verify that:

* ``alembic upgrade head`` creates the ``alembic_version`` table and
  stamps it with the current head revision.
* ``alembic downgrade base`` clears the stamp (the ``alembic_version``
  table itself is preserved by Alembic's bookkeeping).
* ``alembic current`` (i.e. the live revision in the database)
  matches the head revision reported by the script directory.
* Running ``upgrade head`` a second time is a no-op.
* The full upgrade → downgrade cycle leaves the database empty of
  domain tables (only ``alembic_version`` remains).
* The ``created_at`` / ``updated_at`` columns on the four domain
  tables carry ``DEFAULT CURRENT_TIMESTAMP`` after the latest
  migration runs, so an INSERT that omits the timestamps succeeds
  (this is the regression guard for the
  ``NOT NULL constraint failed: credit_cards.created_at`` bug).

The tests are intentionally synchronous: ``alembic.command.upgrade``
and friends are blocking functions that internally drive the
``asyncio`` event loop inside ``env.py`` to reach the async engine.

Phase 1 note
------------
These tests no longer hardcode ``0001_initial`` as the head: the
project now has multiple migrations and the head changes as new
ones are added. The head revision and the full revision list are
read from the :class:`ScriptDirectory` so the tests stay correct
no matter how many migrations exist.
"""

import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
import pytest_asyncio
from alembic.command import downgrade as alembic_downgrade
from alembic.command import upgrade as alembic_upgrade
from alembic.config import Config as AlembicConfig
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import Settings
from app.models import Bank, CreditCard, Statement, Transaction

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ALEMBIC_INI = PROJECT_ROOT / "alembic.ini"
ALEMBIC_DIR = PROJECT_ROOT / "alembic"


def _sync_url(async_url: str) -> str:
    """Translate an ``aiosqlite`` URL into its sync counterpart.

    Alembic's ``env.py`` runs the migrations through the async engine
    using ``asyncio.run`` and ``connection.run_sync``, so the
    connection itself is sync under the hood. For verifying the
    resulting schema state we want a plain synchronous engine pointing
    at the same file — which is just the URL with ``+aiosqlite``
    dropped.
    """
    return async_url.replace("sqlite+aiosqlite", "sqlite", 1)


@pytest.fixture
def alembic_config(test_settings: Settings) -> AlembicConfig:
    """Build an :class:`AlembicConfig` wired to a fresh test database.

    The URL is overridden directly on the config object so we do not
    need to clear the ``get_settings`` cache. The config is rebuilt
    per test (function-scoped) so each test starts from a clean slate.
    """
    cfg = AlembicConfig(str(ALEMBIC_INI))
    cfg.set_main_option("script_location", str(ALEMBIC_DIR))
    cfg.set_main_option("sqlalchemy.url", test_settings.DATABASE_URL)
    return cfg


@pytest.fixture
def sync_engine(test_settings: Settings) -> Iterator[Engine]:
    """Yield a sync :class:`Engine` against the same file as the async one.

    Using a sync engine keeps the post-migration assertions
    (``inspect``) trivial — there is no async context to manage when
    we only need to read the table list.
    """
    engine = create_engine(_sync_url(test_settings.DATABASE_URL))
    try:
        yield engine
    finally:
        engine.dispose()


def _table_names(engine: Engine) -> set[str]:
    """Return the set of table names present in ``engine``'s database."""
    return set(inspect(engine).get_table_names())


def _current_revision(engine: Engine) -> str | None:
    """Return the Alembic revision currently stamped in the database."""
    with engine.connect() as conn:
        ctx = MigrationContext.configure(conn)
        return ctx.get_current_revision()


def test_alembic_upgrade_creates_version_table(
    alembic_config: AlembicConfig, sync_engine: Engine
) -> None:
    """``alembic upgrade head`` creates the ``alembic_version`` table."""
    assert _table_names(sync_engine) == set()

    alembic_upgrade(alembic_config, "head")

    assert "alembic_version" in _table_names(sync_engine)


def test_alembic_current_reports_head_after_upgrade(
    alembic_config: AlembicConfig, sync_engine: Engine
) -> None:
    """``alembic current`` shows the head revision after upgrade.

    The expected head is read from the :class:`ScriptDirectory` so
    the test does not need to be updated every time a new migration
    is added.
    """
    script = ScriptDirectory.from_config(alembic_config)
    expected_head = script.get_current_head()
    assert expected_head is not None

    alembic_upgrade(alembic_config, "head")

    assert _current_revision(sync_engine) == expected_head


def test_alembic_downgrade_clears_version_stamp(
    alembic_config: AlembicConfig, sync_engine: Engine
) -> None:
    """``alembic downgrade base`` succeeds and clears the current revision.

    The ``alembic_version`` table itself is preserved (Alembic manages
    its own bookkeeping), but the stamped revision row is removed so
    ``alembic current`` reports no version afterwards.
    """
    script = ScriptDirectory.from_config(alembic_config)
    expected_head = script.get_current_head()
    assert expected_head is not None

    alembic_upgrade(alembic_config, "head")
    assert "alembic_version" in _table_names(sync_engine)
    assert _current_revision(sync_engine) == expected_head

    alembic_downgrade(alembic_config, "base")

    assert _current_revision(sync_engine) is None
    # The table stays — Alembic keeps an empty placeholder so a
    # subsequent ``upgrade head`` can re-stamp it without recreating
    # the schema. This is the documented Alembic behaviour.
    assert "alembic_version" in _table_names(sync_engine)


def test_alembic_upgrade_is_idempotent(alembic_config: AlembicConfig, sync_engine: Engine) -> None:
    """Running ``upgrade head`` twice is a no-op (no schema change)."""
    alembic_upgrade(alembic_config, "head")
    first_tables = _table_names(sync_engine)

    alembic_upgrade(alembic_config, "head")
    second_tables = _table_names(sync_engine)

    assert first_tables == second_tables
    # ``alembic_version`` is always present; the rest are the
    # domain tables introduced by the migrations.
    assert "alembic_version" in first_tables
    assert len(first_tables) > 1  # at least one domain table


def test_alembic_round_trip_is_reversible(
    alembic_config: AlembicConfig, sync_engine: Engine
) -> None:
    """Upgrade → downgrade reverts to an unstamped, single-table state."""
    script = ScriptDirectory.from_config(alembic_config)
    expected_head = script.get_current_head()
    assert expected_head is not None

    alembic_upgrade(alembic_config, "head")
    assert _current_revision(sync_engine) == expected_head

    alembic_downgrade(alembic_config, "base")
    assert _current_revision(sync_engine) is None
    # Every domain table is gone — only the bookkeeping table
    # remains.
    assert _table_names(sync_engine) == {"alembic_version"}

    # Re-upgrade from the unstamped state works — proves the
    # downgrade didn't leave Alembic in a broken state.
    alembic_upgrade(alembic_config, "head")
    assert _current_revision(sync_engine) == expected_head


def test_alembic_script_directory_has_a_linear_history(
    alembic_config: AlembicConfig,
) -> None:
    """The script directory's revisions form a single linear chain.

    Multiple revisions are expected (Phase 1 added a second one);
    the test now verifies the chain's *shape* rather than its
    size so future migrations don't break it.
    """
    script = ScriptDirectory.from_config(alembic_config)
    revisions = list(script.walk_revisions())

    assert len(revisions) >= 1
    head = script.get_current_head()
    assert head is not None
    # The newest revision (first in ``walk_revisions``) is the
    # current head and has no down-revision pointing past it.
    newest = revisions[0]
    assert newest.revision == head
    assert newest.down_revision is not None
    # The oldest revision (last in the list) is the root of the
    # chain and has no down-revision at all.
    root = revisions[-1]
    assert root.down_revision is None


def test_alembic_seeds_known_banks(alembic_config: AlembicConfig, sync_engine: Engine) -> None:
    """``alembic upgrade head`` seeds the three known Chilean banks.

    The seed is part of the Phase 1 ingestion migration so a fresh
    checkout running ``alembic upgrade head`` ends up with a usable
    database. The test guards against accidental removal of the
    seed when the migration is refactored.
    """
    alembic_upgrade(alembic_config, "head")

    with sync_engine.connect() as conn:
        rows = conn.exec_driver_sql(
            "SELECT name, password_formula, is_active FROM banks ORDER BY name"
        ).fetchall()

    assert len(rows) == 3
    by_name = {name: (formula, active) for name, formula, active in rows}
    assert by_name == {
        "banco_de_chile": ("rut_ultimos_4", 1),
        "itau": ("rut_sin_dv", 1),
        "santander": ("rut_sin_dv", 1),
    }


# ---------------------------------------------------------------------------
# Timestamp server_default regression guard
# ---------------------------------------------------------------------------
#
# The migration ``0002_phase1_ingestion`` created ``created_at`` and
# ``updated_at`` as ``NOT NULL`` columns *without* a ``server_default``
# clause. The ORM model had ``server_default=func.now()``, but the
# database schema Alembic produced did not carry the same default —
# so SQLAlchemy's flush path (which can omit the column on dialects
# with ``RETURNING``) tripped ``NOT NULL constraint failed:
# credit_cards.created_at``.
#
# The regression is fixed by migration ``0004_timestamp_server_defaults``
# (which adds ``DEFAULT CURRENT_TIMESTAMP`` to both columns on every
# domain table) and by the companion change to ``TimestampMixin``
# (which adds the matching ``default=func.now()`` and ``nullable=False``
# on the Python side). The tests below prove both halves of the fix:
# the schema carries the default, and the ORM honours it on flush.


@pytest_asyncio.fixture
async def async_session_factory(
    migrated_test_settings: Settings,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Yield a session factory bound to a freshly-migrated async engine.

    Unlike the ``test_models`` fixture, which uses
    ``Base.metadata.create_all``, this one runs the *real* Alembic
    migration chain (via the sync ``migrated_test_settings``
    fixture, so ``alembic_upgrade`` does not call ``asyncio.run``
    inside our event loop). The point of the regression guard is to
    verify the migration produces a schema that lets the
    application insert rows without explicit timestamps —
    recreating the schema with ``create_all`` would mask the bug
    entirely (the ORM model itself already had the right
    ``server_default``).
    """
    eng: AsyncEngine = create_async_engine(migrated_test_settings.DATABASE_URL)
    try:
        factory = async_sessionmaker(eng, expire_on_commit=False)
        yield factory
    finally:
        await eng.dispose()


@pytest.fixture
def migrated_test_settings(test_settings: Settings) -> Settings:
    """Run ``alembic upgrade head`` on the test database, return the settings.

    This is intentionally a *sync* fixture: ``alembic.command.upgrade``
    calls ``asyncio.run`` internally (see ``alembic/env.py``) and
    that call would fail with ``RuntimeError: asyncio.run() cannot
    be called from a running event loop`` if invoked from inside
    a ``pytest-asyncio`` test. Keeping the migration in a sync
    fixture lets Alembic drive its own event loop on a clean slate.
    """
    cfg = AlembicConfig(str(ALEMBIC_INI))
    cfg.set_main_option("script_location", str(ALEMBIC_DIR))
    cfg.set_main_option("sqlalchemy.url", test_settings.DATABASE_URL)
    alembic_upgrade(cfg, "head")
    return test_settings


def test_alembic_timestamp_columns_have_server_default(
    alembic_config: AlembicConfig, sync_engine: Engine
) -> None:
    """The four domain tables carry ``DEFAULT CURRENT_TIMESTAMP`` on timestamps.

    Inspects the live database schema (after ``upgrade head``) and
    asserts that the ``server_default`` of every ``created_at`` and
    ``updated_at`` column is non-``None``. This is the *schema-level*
    half of the fix: without this, SQLAlchemy's flush path on SQLite
    (which relies on ``RETURNING`` and omits server-defaulted
    columns) hits the ``NOT NULL`` constraint.
    """
    alembic_upgrade(alembic_config, "head")
    inspector = inspect(sync_engine)

    for table in ("banks", "credit_cards", "statements", "transactions"):
        columns = {col["name"]: col for col in inspector.get_columns(table)}
        for ts_col in ("created_at", "updated_at"):
            assert ts_col in columns, f"{table}.{ts_col} missing from schema"
            default = columns[ts_col].get("default")
            assert default is not None, (
                f"{table}.{ts_col} has no server default — "
                f"the upload endpoint will hit NOT NULL constraint failure"
            )
            # The default is rendered as a SQL fragment; ``CURRENT_TIMESTAMP``
            # is what ``sa.func.now()`` compiles to on every dialect this
            # project supports (SQLite and PostgreSQL). Matching on the
            # substring keeps the test portable across both.
            assert "CURRENT_TIMESTAMP" in str(default).upper(), (
                f"{table}.{ts_col} default is {default!r}, expected CURRENT_TIMESTAMP"
            )


@pytest.mark.asyncio
async def test_credit_card_creation_without_explicit_timestamps(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A ``CreditCard`` inserted on a migrated DB gets timestamps from the DB.

    Regression guard for ``NOT NULL constraint failed:
    credit_cards.created_at``. Before migration ``0004``, the
    application raised 500 on every upload because the database
    schema lacked the ``DEFAULT CURRENT_TIMESTAMP`` clause. With
    the migration in place, the same INSERT succeeds and the row
    carries non-``None`` ``created_at`` / ``updated_at`` populated
    by the engine.
    """
    async with async_session_factory() as session:
        # Seed a bank first (banks is the parent of credit_cards).
        # The bank row also goes through the timestamp path — the
        # seed in ``0002`` writes explicit timestamps, so this row
        # is just a control to make sure the fixture is alive.
        bank = Bank(
            id=uuid.UUID("11111111-1111-1111-1111-111111111111"),
            name="repro_bank",
            display_name="Repro Bank",
            password_formula="rut_sin_dv",
        )
        session.add(bank)
        await session.commit()

        # The card is the *real* subject of the regression: pre-fix
        # this ``commit()`` raised IntegrityError because the DB
        # schema had ``NOT NULL created_at`` with no default.
        card = CreditCard(
            bank_id=bank.id,
            card_number_masked="XXXX XXXX XXXX 1234",
            cardholder="REPRO USER",
            currency="CLP",
        )
        session.add(card)
        await session.commit()  # was raising before the fix
        await session.refresh(card)

        assert card.id is not None
        assert card.created_at is not None, "created_at not populated by DB default"
        assert card.updated_at is not None, "updated_at not populated by DB default"
        assert isinstance(card.created_at, type(card.updated_at))
        assert card.created_at <= card.updated_at


@pytest.mark.asyncio
async def test_all_timestamp_mixin_tables_accept_timeless_inserts(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Every model that opts into ``TimestampMixin`` accepts a timestamp-less insert.

    The ``TimestampMixin`` is shared across the four domain tables,
    so the fix must hold for *all* of them — not just
    ``credit_cards``. This test inserts one row per table (in
    dependency order) and asserts each commit succeeds and the
    timestamps are populated. The parent rows are seeded with
    explicit UUIDs to keep the test self-contained.
    """
    bank_id = uuid.UUID("22222222-2222-2222-2222-222222222222")
    card_id = uuid.UUID("33333333-3333-3333-3333-333333333333")
    statement_id = uuid.UUID("44444444-4444-4444-4444-444444444444")

    async with async_session_factory() as session:
        # Bank
        bank = Bank(
            id=bank_id,
            name="repro_bank_all",
            display_name="Repro All",
            password_formula="rut_sin_dv",
        )
        session.add(bank)
        await session.commit()
        await session.refresh(bank)
        assert bank.created_at is not None
        assert bank.updated_at is not None

        # CreditCard
        card = CreditCard(
            id=card_id,
            bank_id=bank.id,
            card_number_masked="XXXX XXXX XXXX 9999",
            cardholder="REPRO ALL",
            currency="CLP",
        )
        session.add(card)
        await session.commit()
        await session.refresh(card)
        assert card.created_at is not None
        assert card.updated_at is not None

        # Statement
        statement = Statement(
            id=statement_id,
            credit_card_id=card.id,
            period_start=date(2026, 5, 1),
            period_end=date(2026, 5, 31),
            statement_date=date(2026, 6, 1),
            file_path="repro/2026-05.pdf",
            file_hash="a" * 64,
        )
        session.add(statement)
        await session.commit()
        await session.refresh(statement)
        assert statement.created_at is not None
        assert statement.updated_at is not None

        # Transaction (the deepest child)
        tx = Transaction(
            statement_id=statement.id,
            date=date(2026, 5, 5),
            description="REPRO TX",
            amount=Decimal("100.00"),
            currency="CLP",
        )
        session.add(tx)
        await session.commit()
        await session.refresh(tx)
        assert tx.created_at is not None
        assert tx.updated_at is not None
