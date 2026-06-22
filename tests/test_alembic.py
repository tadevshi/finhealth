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

from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic.command import downgrade as alembic_downgrade
from alembic.command import upgrade as alembic_upgrade
from alembic.config import Config as AlembicConfig
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect
from sqlalchemy.engine import Engine

from app.core.config import Settings

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
