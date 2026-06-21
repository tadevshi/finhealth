"""Async startup/shutdown lifespan for the FastAPI application.

The :func:`create_lifespan` factory returns an async context manager
that FastAPI's ``lifespan=`` argument understands. On startup the
factory creates the application engine, validates database
connectivity with a ``SELECT 1`` round-trip, and stores the engine on
``app.state.engine`` so it can be inspected or reused by handlers
that need a low-level connection. On shutdown the engine is disposed
and its connection pool released.

A failure during the startup probe is intentionally propagated: if the
database is unreachable the app is no better than a process that
serves 503s, and a hard fail is the right signal to a supervisor like
systemd, Docker, or Kubernetes.
"""

import logging
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager

from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.core.config import Settings
from app.db.engine import create_engine

logger = logging.getLogger(__name__)

Lifespan = Callable[[FastAPI], AbstractAsyncContextManager[None]]


def create_lifespan(settings: Settings) -> Lifespan:
    """Build a FastAPI lifespan that warms up and tears down the DB engine.

    Parameters
    ----------
    settings:
        Application settings providing ``DATABASE_URL`` and ``APP_NAME``.
        The same settings object used to build the FastAPI app is
        passed in, keeping the lifespan, app, and dependency chain
        pointed at the same database.

    Returns
    -------
    Lifespan
        A callable that, when given a :class:`FastAPI` app, returns an
        async context manager suitable for the ``lifespan=`` argument.
    """

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # ---- Startup -----------------------------------------------------
        logger.info("Starting %s (database=%s)", settings.APP_NAME, settings.DATABASE_URL)
        engine = create_engine(settings)
        app.state.engine = engine
        try:
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
        except SQLAlchemyError:
            logger.exception("Database connectivity check failed for %s", settings.DATABASE_URL)
            await engine.dispose()
            raise
        logger.info("Database connectivity verified")
        yield
        # ---- Shutdown ----------------------------------------------------
        logger.info("Shutting down %s", settings.APP_NAME)
        await engine.dispose()
        logger.info("Database engine disposed")

    return lifespan
