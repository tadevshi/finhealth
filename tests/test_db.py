"""Smoke tests for the database layer.

These tests exercise the building blocks that Work Unit 2 introduces:

* :class:`app.models.base.Base` — declarative base registration.
* :class:`app.models.mixins.UUIDType` — UUID ↔ str conversion.
* :class:`app.models.mixins.UUIDMixin` and
  :class:`app.models.mixins.TimestampMixin` — column declarations.
 * :func:`app.db.engine.create_engine` — async PostgreSQL engine.
* :func:`app.db.session.create_session_factory` — session factory.
* :func:`app.db.session.get_session` — FastAPI dependency with
  commit/rollback semantics.

 All database checks use the disposable PostgreSQL database provided by
 :mod:`tests.conftest`.
"""

import uuid

import pytest
from sqlalchemy import String, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.core.config import Settings
from app.db.engine import create_engine
from app.db.session import create_session_factory, get_session
from app.models.base import Base
from app.models.mixins import TimestampMixin, UUIDMixin, UUIDType

# ---------------------------------------------------------------------------
# Pure-Python checks (no DB)
# ---------------------------------------------------------------------------


def test_base_is_declarative_subclass() -> None:
    """``Base`` is a SQLAlchemy 2.0 :class:`DeclarativeBase` subclass."""
    assert isinstance(Base, type)
    assert issubclass(Base, DeclarativeBase)


def test_uuid_type_binds_canonical_string() -> None:
    """``UUIDType`` converts a UUID to its canonical 36-char string form."""
    test_uuid = uuid.uuid4()
    bound = UUIDType().process_bind_param(test_uuid, dialect=None)

    assert bound == str(test_uuid)
    assert len(bound) == 36  # canonical UUID string length


def test_uuid_type_binds_none_as_none() -> None:
    """``UUIDType`` round-trips ``None`` as ``None`` on bind."""
    assert UUIDType().process_bind_param(None, dialect=None) is None


def test_uuid_type_reads_uuid_from_string() -> None:
    """``UUIDType`` converts a stored string back to a ``UUID`` instance."""
    test_uuid = uuid.uuid4()
    result = UUIDType().process_result_value(str(test_uuid), dialect=None)

    assert result == test_uuid
    assert isinstance(result, uuid.UUID)


def test_uuid_type_reads_none_as_none() -> None:
    """``UUIDType`` round-trips ``None`` as ``None`` on result."""
    assert UUIDType().process_result_value(None, dialect=None) is None


def test_mixins_expose_mapped_columns() -> None:
    """Mixins declare ``id``, ``created_at``, and ``updated_at`` as Mapped attrs."""
    assert "id" in UUIDMixin.__annotations__
    assert "created_at" in TimestampMixin.__annotations__
    assert "updated_at" in TimestampMixin.__annotations__


# ---------------------------------------------------------------------------
# Engine + session smoke tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_engine_connects_to_postgresql(test_settings: Settings) -> None:
    """``create_engine`` opens a working PostgreSQL connection."""
    engine = create_engine(test_settings.database_url)
    try:
        async with engine.connect() as conn:
            result = await conn.execute(text("SELECT 1"))
            assert result.scalar_one() == 1
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_session_factory_yields_async_session(test_settings: Settings) -> None:
    """``create_session_factory`` produces a working ``AsyncSession``."""
    engine = create_engine(test_settings.database_url)
    try:
        factory = create_session_factory(engine)
        async with factory() as session:
            assert isinstance(session, AsyncSession)
            result = await session.execute(text("SELECT 1"))
            assert result.scalar_one() == 1
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# get_session dependency tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_session_yields_async_session(test_settings: Settings) -> None:
    """``get_session`` yields a usable ``AsyncSession`` on first iteration."""
    gen = get_session(settings=test_settings)
    try:
        session = await gen.__anext__()
        assert isinstance(session, AsyncSession)
        result = await session.execute(text("SELECT 1"))
        assert result.scalar_one() == 1
    finally:
        await gen.aclose()


@pytest.mark.asyncio
async def test_get_session_rolls_back_on_exception(test_settings: Settings) -> None:
    """``get_session`` rolls back the session and re-raises handler errors.

    The test drives the dependency directly: an exception is injected
    at the yield point via :func:`athrow`, which mimics what FastAPI
    does when a route handler raises. The dependency must roll back
    the in-flight transaction and re-raise the original error.
    """
    gen = get_session(settings=test_settings)
    session = await gen.__anext__()

    # Open a real transaction so rollback has something to undo.
    await session.execute(text("BEGIN"))
    assert session.in_transaction() is True

    with pytest.raises(RuntimeError, match="boom"):
        await gen.athrow(RuntimeError("boom"))

    # The dependency's except branch called rollback — the transaction
    # is no longer active.
    assert session.in_transaction() is False
    await gen.aclose()


@pytest.mark.asyncio
async def test_get_session_closes_engine_on_exit(test_settings: Settings) -> None:
    """The dependency's generator is single-use and closes on :func:`aclose`."""
    gen = get_session(settings=test_settings)
    await gen.__anext__()
    await gen.aclose()

    # A second iteration on a closed dependency must raise StopAsyncIteration.
    with pytest.raises(StopAsyncIteration):
        await gen.__anext__()


# ---------------------------------------------------------------------------
# End-to-end: declare a model with Base + mixins and persist a row
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_base_can_declare_and_persist_model(test_settings: Settings) -> None:
    """A model using ``Base`` + the mixins creates a table and round-trips a row."""
    from datetime import datetime

    class Sample(Base, UUIDMixin, TimestampMixin):
        __tablename__ = "samples"
        name: Mapped[str] = mapped_column(String(50))

    engine = create_engine(test_settings.database_url)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        try:
            factory = create_session_factory(engine)
            async with factory() as session:
                sample = Sample(name="hello")
                session.add(sample)
                await session.commit()
                await session.refresh(sample)

                assert isinstance(sample.id, uuid.UUID)
                assert sample.name == "hello"
                assert isinstance(sample.created_at, datetime)
                assert isinstance(sample.updated_at, datetime)
                # created_at <= updated_at right after insert
                assert sample.created_at <= sample.updated_at
        finally:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.drop_all)
    finally:
        await engine.dispose()
