"""Shared pytest fixtures for the integration test suite.

The fixtures below provide a clean, isolated application instance for
every test:

* :func:`test_settings` points ``DATABASE_URL`` at a temporary file
  inside a fresh :class:`tempfile.TemporaryDirectory`. This avoids
  any cross-test pollution of the SQLite database.
* :func:`engine` creates the engine used by the health probe and
  disposes it after the test.
* :func:`client` builds the FastAPI app against the test settings and
  wires an :class:`httpx.AsyncClient` with :class:`ASGITransport`
  so requests are handled in-process — no real network I/O.

The lifespan event is *not* triggered by ``ASGITransport`` (httpx's
default), so the test does not exercise the production startup/shutdown
hooks. This is intentional: the health endpoint uses the
``get_session`` dependency directly, not ``app.state.engine``, so the
lifespan would only add noise. Lifespan behaviour is covered by the
``test_lifespan.py`` unit test (added when lifespan-specific edge
cases warrant it).
"""

import tempfile
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.core.config import Settings, get_settings
from app.main import create_app


@pytest.fixture
def test_settings(monkeypatch: pytest.MonkeyPatch) -> Iterator[Settings]:
    """Yield a :class:`Settings` pointing at a throwaway SQLite file.

    The previous cached settings are restored between tests via
    ``monkeypatch`` and explicit ``get_settings.cache_clear()`` calls
    so cached singletons from other fixtures cannot leak across
    tests.
    """
    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = Path(tmp_dir) / "finhealth-test.db"
        monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_path}")
        monkeypatch.setenv("CORS_ORIGINS", '["http://localhost", "http://testserver"]')
        get_settings.cache_clear()
        try:
            yield get_settings()
        finally:
            get_settings.cache_clear()


@pytest.fixture
async def engine(test_settings: Settings) -> AsyncIterator[AsyncEngine]:
    """Yield an :class:`AsyncEngine` bound to the test database file."""
    eng = create_async_engine(
        test_settings.DATABASE_URL,
        echo=False,
        connect_args={"check_same_thread": False},
    )
    try:
        yield eng
    finally:
        await eng.dispose()


@pytest.fixture
async def client(test_settings: Settings) -> AsyncIterator[AsyncClient]:
    """Yield an :class:`AsyncClient` wired to a fresh FastAPI app.

    The app is created with the test settings so its lifespan,
    CORS, and ``get_session`` dependency all point at the temporary
    database. ``ASGITransport`` handles requests in-process.
    """
    app = create_app(test_settings)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac
