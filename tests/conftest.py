"""Shared PostgreSQL fixtures for the test suite."""

import asyncio
import os
import uuid
from collections.abc import AsyncIterator, Iterator

import asyncpg
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.config import Settings, get_settings
from app.db.engine import create_engine
from app.main import create_app
from app.models.base import Base


def _test_connection_settings() -> tuple[str, int, str, str, str]:
    """Read the explicit PostgreSQL administrator connection for isolated tests."""
    host = os.getenv("POSTGRES_TEST_HOST")
    if not host:
        pytest.skip("POSTGRES_TEST_HOST is required for PostgreSQL-backed tests")
    return (
        host,
        int(os.getenv("POSTGRES_TEST_PORT", "5432")),
        os.getenv("POSTGRES_TEST_USER", "finhealth"),
        os.getenv("POSTGRES_TEST_PASSWORD", "secret"),
        os.getenv("POSTGRES_TEST_ADMIN_DB", "postgres"),
    )


async def _create_database(
    host: str, port: int, user: str, password: str, admin_database: str, database: str
) -> None:
    connection = await asyncpg.connect(
        host=host, port=port, user=user, password=password, database=admin_database
    )
    try:
        await connection.execute(f'CREATE DATABASE "{database}"')
    finally:
        await connection.close()


async def _drop_database(
    host: str, port: int, user: str, password: str, admin_database: str, database: str
) -> None:
    connection = await asyncpg.connect(
        host=host, port=port, user=user, password=password, database=admin_database
    )
    try:
        await connection.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname = $1 AND pid <> pg_backend_pid()",
            database,
        )
        await connection.execute(f'DROP DATABASE IF EXISTS "{database}"')
    finally:
        await connection.close()


@pytest.fixture
def test_settings(monkeypatch: pytest.MonkeyPatch) -> Iterator[Settings]:
    """Yield settings for a disposable PostgreSQL database, then drop it."""
    host, port, user, password, admin_database = _test_connection_settings()
    database = f"finhealth_test_{uuid.uuid4().hex}"
    asyncio.run(_create_database(host, port, user, password, admin_database, database))
    monkeypatch.setenv("POSTGRES_HOST", host)
    monkeypatch.setenv("POSTGRES_PORT", str(port))
    monkeypatch.setenv("POSTGRES_USER", user)
    monkeypatch.setenv("POSTGRES_PASSWORD", password)
    monkeypatch.setenv("POSTGRES_DB", database)
    monkeypatch.setenv("CORS_ORIGINS", '["http://localhost", "http://testserver"]')
    get_settings.cache_clear()
    try:
        yield get_settings()
    finally:
        get_settings.cache_clear()
        asyncio.run(_drop_database(host, port, user, password, admin_database, database))


@pytest.fixture
async def engine(test_settings: Settings) -> AsyncIterator[AsyncEngine]:
    """Yield an engine with the full ORM schema in the isolated database."""
    test_engine = create_engine(test_settings.database_url)
    try:
        async with test_engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        yield test_engine
    finally:
        await test_engine.dispose()


@pytest.fixture
async def client(test_settings: Settings) -> AsyncIterator[AsyncClient]:
    """Yield an in-process client backed by an isolated PostgreSQL database."""
    app = create_app(test_settings)
    bootstrap_engine = create_engine(test_settings.database_url)
    try:
        async with bootstrap_engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
    finally:
        await bootstrap_engine.dispose()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as test_client:
        yield test_client
