"""Async session factory and FastAPI dependency.

The session factory is the only object the rest of the app should
touch for ORM work. The :func:`get_session` dependency is the FastAPI
integration point: it yields a fresh :class:`AsyncSession` per
request, rolls back on exception, and disposes the engine on exit.

Note on commit semantics: this dependency intentionally does **not**
auto-commit on clean exit. FastAPI closes generator dependencies by
calling :func:`aclose`, which raises :class:`GeneratorExit` at the
``yield`` — that is a :class:`BaseException`, not :class:`Exception`,
so a ``try/except/else`` commit pattern would never fire. Route
handlers must call ``await session.commit()`` explicitly when they
want to persist changes; this is the standard SQLAlchemy + FastAPI
pattern and keeps transaction boundaries obvious at the call site.
"""

from collections.abc import AsyncGenerator
from typing import Annotated

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.core.config import Settings, get_settings
from app.db.engine import create_engine


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Build a session factory bound to ``engine``.

    ``expire_on_commit`` is disabled so attribute access on instances
    is valid after the session commits — required for FastAPI response
    serialization.
    """
    return async_sessionmaker(engine, expire_on_commit=False)


async def get_session(
    settings: Annotated[Settings, Depends(get_settings)],
) -> AsyncGenerator[AsyncSession, None]:
    """Yield an :class:`AsyncSession` scoped to one FastAPI request.

    The session is created from a fresh engine and session factory so
    that requests are isolated by settings (useful for tests that
    override ``DATABASE_URL``). The engine is disposed when the
    generator exits, releasing the connection pool.

    If the consuming code raises inside the FastAPI request, the
    session rolls back the active transaction before propagating the
    exception. On a clean exit the session is simply closed — pending
    transactions are discarded, so handlers that intend to persist
    must call ``await session.commit()`` themselves.
    """
    engine = create_engine(settings)
    factory = create_session_factory(engine)
    try:
        async with factory() as session:
            try:
                yield session
            except Exception:
                await session.rollback()
                raise
    finally:
        await engine.dispose()
