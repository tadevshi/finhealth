"""Async SQLAlchemy engine factory with SQLite-friendly defaults.

The engine is created per call to :func:`create_engine` and is
disposable via ``engine.dispose()``. For a long-running process the
caller is expected to cache the engine (e.g. on ``app.state`` in the
FastAPI lifespan) to amortize connection-pool cost. SQLite connections
are also wired with ``PRAGMA journal_mode=WAL`` for better concurrent
read behavior.
"""

from typing import TYPE_CHECKING, Any

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.core.config import Settings

if TYPE_CHECKING:
    from sqlalchemy.engine.interfaces import DBAPIConnection


def _enable_sqlite_wal(
    dbapi_connection: "DBAPIConnection",
    _connection_record: Any,
) -> None:
    """Enable WAL journal mode on every new SQLite connection.

    The listener is attached only when the engine's dialect is SQLite,
    so calling :func:`create_engine` with a non-SQLite URL is a no-op
    for this hook.
    """
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA journal_mode=WAL")
    finally:
        cursor.close()


def create_engine(settings: Settings) -> AsyncEngine:
    """Build an :class:`AsyncEngine` configured from ``settings``.

    The engine reads its connection string from ``settings.DATABASE_URL``
    and enables ``SQLALCHEMY_ECHO`` when ``settings.DEBUG`` is true.
    For SQLite, the engine additionally registers a connect-listener
    that issues ``PRAGMA journal_mode=WAL`` so every pooled connection
    is in WAL mode.

    Parameters
    ----------
    settings:
        Application settings providing ``DATABASE_URL`` and ``DEBUG``.

    Returns
    -------
    AsyncEngine
        A ready-to-use async engine. The caller owns its lifecycle and
        must call ``engine.dispose()`` when done.
    """
    engine = create_async_engine(
        settings.DATABASE_URL,
        echo=settings.DEBUG,
        connect_args={"check_same_thread": False},
    )

    if engine.dialect.name == "sqlite":
        event.listen(engine.sync_engine, "connect", _enable_sqlite_wal)

    return engine
