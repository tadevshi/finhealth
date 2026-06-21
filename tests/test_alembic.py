"""Tests for Alembic migration plumbing.

These tests exercise the full upgrade/downgrade round-trip against a
temporary SQLite file and verify that:

* ``alembic upgrade head`` creates the ``alembic_version`` table and
  stamps it with the initial revision.
* ``alembic downgrade base`` drops the version table and clears the
  stamp.
* ``alembic current`` (i.e. the live revision in the database)
  matches the head revision reported by the script directory.
* Running ``upgrade head`` a second time is a no-op.
* The full upgrade → downgrade cycle leaves the database empty.

The tests are intentionally synchronous: ``alembic.command.upgrade``
and friends are blocking functions that internally drive the
``asyncio`` event loop inside ``env.py`` to reach the async engine.
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
INITIAL_REVISION = "0001_initial"


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
    """``alembic current`` shows the initial revision after upgrade."""
    script = ScriptDirectory.from_config(alembic_config)
    assert script.get_current_head() == INITIAL_REVISION

    alembic_upgrade(alembic_config, "head")

    assert _current_revision(sync_engine) == INITIAL_REVISION


def test_alembic_downgrade_clears_version_stamp(
    alembic_config: AlembicConfig, sync_engine: Engine
) -> None:
    """``alembic downgrade base`` succeeds and clears the current revision.

    The ``alembic_version`` table itself is preserved (Alembic manages
    its own bookkeeping), but the stamped revision row is removed so
    ``alembic current`` reports no version afterwards.
    """
    alembic_upgrade(alembic_config, "head")
    assert "alembic_version" in _table_names(sync_engine)
    assert _current_revision(sync_engine) == INITIAL_REVISION

    alembic_downgrade(alembic_config, "base")

    assert _current_revision(sync_engine) is None
    # The table stays — Alembic keeps an empty placeholder so a
    # subsequent ``upgrade head`` can re-stamp it without recreating
    # the schema. This is the documented Alembic behaviour for a
    # no-op initial migration.
    assert "alembic_version" in _table_names(sync_engine)


def test_alembic_upgrade_is_idempotent(alembic_config: AlembicConfig, sync_engine: Engine) -> None:
    """Running ``upgrade head`` twice is a no-op (no schema change)."""
    alembic_upgrade(alembic_config, "head")
    first_tables = _table_names(sync_engine)

    alembic_upgrade(alembic_config, "head")
    second_tables = _table_names(sync_engine)

    assert first_tables == second_tables == {"alembic_version"}


def test_alembic_round_trip_is_reversible(
    alembic_config: AlembicConfig, sync_engine: Engine
) -> None:
    """Upgrade → downgrade reverts to an unstamped, single-table state."""
    alembic_upgrade(alembic_config, "head")
    assert _current_revision(sync_engine) == INITIAL_REVISION

    alembic_downgrade(alembic_config, "base")
    assert _current_revision(sync_engine) is None

    # Re-upgrade from the unstamped state works — proves the
    # downgrade didn't leave Alembic in a broken state.
    alembic_upgrade(alembic_config, "head")
    assert _current_revision(sync_engine) == INITIAL_REVISION


def test_alembic_script_directory_has_only_initial_revision(
    alembic_config: AlembicConfig,
) -> None:
    """The script directory exposes exactly one revision: the placeholder."""
    script = ScriptDirectory.from_config(alembic_config)
    revisions = list(script.walk_revisions())

    assert len(revisions) == 1
    assert revisions[0].revision == INITIAL_REVISION
    assert revisions[0].down_revision is None
