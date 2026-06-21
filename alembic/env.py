"""Alembic environment — async SQLAlchemy migration runner.

This module is loaded by the Alembic CLI (``alembic upgrade``, etc.)
and configures the migration context to talk to the same database the
FastAPI application uses. Two design decisions worth flagging:

1. **Source of truth is the application settings**, not
   ``alembic.ini``. ``alembic.ini`` leaves ``sqlalchemy.url`` blank and
   the URL is read from :func:`app.core.config.get_settings`, which
   honours the ``DATABASE_URL`` environment variable. This keeps dev,
   test, and production in lock-step with the FastAPI app.
2. **Migrations run on an async engine** (``aiosqlite`` for now) via
   :func:`sqlalchemy.ext.asyncio.async_engine_from_config`. The
   ``run_migrations_online`` function drives an ``asyncio.run`` loop,
   so the CLI invocation is still synchronous from Alembic's
   perspective — every async SQLAlchemy call goes through
   ``connection.run_sync`` to run sync-style inside the event loop.
"""

import asyncio
from logging.config import fileConfig

from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context

# ---------------------------------------------------------------------------
# Project imports
# ---------------------------------------------------------------------------
# Importing ``app.core.config`` and ``app.models.base`` ensures Alembic
# sees the same declarative ``Base.metadata`` as the running app. The
# import is intentionally *after* the standard-library / third-party
# imports so isort groups them correctly. ``app`` is on ``sys.path``
# thanks to ``prepend_sys_path = .`` in ``alembic.ini``.
from app.core.config import get_settings
from app.models.base import Base

# This is the Alembic Config object, which provides access to the
# values within the .ini file in use.
config = context.config

# Interpret the config file for Python logging.
# This line sets up loggers basically.
#
# ``disable_existing_loggers=False`` is required so the migration
# runner does not clobber handlers installed by other systems (most
# importantly, pytest's ``caplog`` fixture, which is wired at the
# root logger). The Python logging spec defaults this to ``True``
# for safety; we override it because the alembic logger tree is
# a small, well-known subset and we never want to surprise other
# tools.
if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

# Inject the runtime database URL from application settings. This
# overrides whatever ``sqlalchemy.url`` is set to in ``alembic.ini``,
# so the value declared in the INI file is effectively a placeholder.
settings = get_settings()
config.set_main_option("sqlalchemy.url", settings.DATABASE_URL)

# Metadata ``autogenerate`` reads when building new migrations.
# Empty for now — no domain models exist yet.
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    In this mode Alembic emits SQL scripts to stdout without talking
    to a live database. The URL is read from the (already-injected)
    main option. ``render_as_batch`` is enabled so SQLite gets the
    ``ALTER TABLE`` rewrites it needs (e.g. column renames), even
    though this project does not use autogenerate yet.
    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    """Run migrations inside a sync ``Connection`` (async-safe)."""
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        render_as_batch=True,
    )

    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """Drive migrations on an :class:`AsyncEngine`.

    The engine is built from the Alembic config (which has just had
    its ``sqlalchemy.url`` rewritten above) and used as a context
    manager so the connection pool is released cleanly.
    """
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode using the async engine."""
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
