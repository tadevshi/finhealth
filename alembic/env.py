"""PostgreSQL-only Alembic runner using the application URL object."""

import asyncio
from logging.config import fileConfig

import sqlalchemy as sa
from alembic.script import ScriptDirectory
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

from alembic import context
from app.core.config import get_settings
from app.models.base import Base

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

settings = get_settings()
target_metadata = Base.metadata

_USER_TABLES = sa.text(
    """
    SELECT n.nspname || '.' || c.relname
    FROM pg_catalog.pg_class AS c
    JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
    WHERE c.relkind IN ('r', 'p')
      AND n.nspname <> 'information_schema'
      AND n.nspname NOT LIKE 'pg_%'
      AND c.relname <> 'alembic_version'
    ORDER BY n.nspname, c.relname
    """
)


def run_migrations_offline() -> None:
    """Emit PostgreSQL SQL using the same structured settings URL."""
    context.configure(
        url=settings.database_url.render_as_string(hide_password=False),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    """Refuse non-empty user databases before a destructive baseline runs."""
    if connection.dialect.name != "postgresql":
        raise RuntimeError("PostgreSQL is required for Alembic migrations")

    context.configure(connection=connection, target_metadata=target_metadata)
    migration_context = context.get_context()
    script_head = ScriptDirectory.from_config(config).get_current_head()
    if migration_context.get_current_revision() == script_head:
        return

    user_tables = list(connection.execute(_USER_TABLES).scalars())
    if user_tables:
        raise RuntimeError(
            "Refusing to initialize non-empty database; found user tables: "
            + ", ".join(user_tables)
        )

    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """Run migrations on an async engine constructed from the URL object."""
    connectable = create_async_engine(settings.database_url)
    try:
        async with connectable.connect() as connection:
            await connection.run_sync(do_run_migrations)
    finally:
        await connectable.dispose()


def run_migrations_online() -> None:
    """Run the PostgreSQL migration flow from Alembic's synchronous CLI."""
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
