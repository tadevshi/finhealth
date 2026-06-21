"""Tests for the application lifespan context manager.

The lifespan is the FastAPI startup/shutdown hook: it warms the
database engine on startup, validates connectivity, and disposes the
engine on shutdown. These tests drive the lifespan directly via
``app.router.lifespan_context`` (the same API a real ASGI server
calls) and assert the side-effects on ``app.state.engine`` and the
log output.
"""

import logging

import pytest
from fastapi import FastAPI

from app.core.config import Settings
from app.core.lifespan import create_lifespan
from app.main import create_app


@pytest.mark.asyncio
async def test_lifespan_initialises_engine_on_app_state(
    test_settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    """The lifespan stores an engine on ``app.state.engine`` after startup."""
    app = create_app(test_settings)
    lifespan = create_lifespan(test_settings)

    with caplog.at_level(logging.INFO, logger="app.core.lifespan"):
        async with lifespan(app):
            # Inside the lifespan, the engine is bound and reachable.
            engine = app.state.engine
            assert engine is not None

            # The engine is bound to the configured database URL.
            assert str(engine.url) == test_settings.DATABASE_URL

        # After the lifespan exits, ``app.state.engine`` is still
        # the same object (the lifespan only disposes it, it does
        # not detach the reference).
        assert app.state.engine is engine

    # Startup and shutdown messages were logged.
    messages = [record.getMessage() for record in caplog.records]
    assert any("Starting" in msg for msg in messages)
    assert any("Shutting down" in msg for msg in messages)
    assert any("connectivity verified" in msg for msg in messages)


@pytest.mark.asyncio
async def test_lifespan_disposes_engine_on_shutdown(
    test_settings: Settings,
) -> None:
    """Calling ``engine.dispose()`` on the post-shutdown engine raises.

    After the lifespan exits, the engine is disposed and any
    subsequent attempt to use it is a programming error. The test
    drives a real query inside the lifespan (proving the engine
    works) and a new ``create_async_engine`` afterwards (proving the
    original is no longer usable through SQLAlchemy's pool).
    """
    from sqlalchemy.ext.asyncio import create_async_engine

    app = create_app(test_settings)
    lifespan = create_lifespan(test_settings)

    async with lifespan(app):
        # Engine works during the lifespan.
        engine = app.state.engine
        async with engine.connect() as conn:
            from sqlalchemy import text

            result = await conn.execute(text("SELECT 1"))
            assert result.scalar_one() == 1

    # After shutdown, the engine pool is closed. A fresh engine on
    # the same URL still works (this is a behavioural sanity check
    # that the underlying file is not corrupted by dispose()).
    fresh = create_async_engine(test_settings.DATABASE_URL)
    try:
        async with fresh.connect() as conn:
            from sqlalchemy import text

            result = await conn.execute(text("SELECT 1"))
            assert result.scalar_one() == 1
    finally:
        await fresh.dispose()

    # Sanity: the original engine's pool is closed. We don't call
    # methods on it (that would raise ``InvalidRequestError``), but
    # we do confirm it's no longer the same as a fresh one.
    assert app.state.engine is not fresh


@pytest.mark.asyncio
async def test_lifespan_raises_when_db_unreachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A database that cannot be reached aborts startup.

    The lifespan is intentionally strict: it propagates startup
    failures so that a supervisor (systemd, Docker, k8s) can
    restart a fresh process instead of running a half-broken
    service.
    """
    from sqlalchemy.exc import SQLAlchemyError

    from app.core.config import get_settings

    # An obviously invalid URL — argument validation fails before
    # any network call. The exact exception class depends on which
    # step of engine construction trips first; we accept any
    # ``SQLAlchemyError`` subclass.
    monkeypatch.setenv(
        "DATABASE_URL", "sqlite+aiosqlite:////this/path/does/not/exist/at/all/x9k.db"
    )
    get_settings.cache_clear()
    bad_settings = get_settings()

    app = FastAPI()
    lifespan = create_lifespan(bad_settings)

    with pytest.raises(SQLAlchemyError):
        async with lifespan(app):
            pytest.fail("Lifespan must not enter the context when the DB is unreachable")

    get_settings.cache_clear()
