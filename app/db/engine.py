"""Async PostgreSQL SQLAlchemy engine factory.

The engine is created per call to :func:`create_engine` and is
disposable via ``engine.dispose()``. For a long-running process the
caller is expected to cache the engine (e.g. on ``app.state`` in the
FastAPI lifespan) to amortize connection-pool cost.
"""

from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine


def create_engine(database_url: URL, *, debug: bool = False) -> AsyncEngine:
    """Build an :class:`AsyncEngine` from a structured PostgreSQL URL.

    Parameters
    ----------
    database_url:
        Validated SQLAlchemy URL returned by ``Settings.database_url``.
    debug:
        Enable SQLAlchemy statement logging.

    Returns
    -------
    AsyncEngine
        A ready-to-use async engine. The caller owns its lifecycle and
        must call ``engine.dispose()`` when done.
    """
    return create_async_engine(database_url, echo=debug)
