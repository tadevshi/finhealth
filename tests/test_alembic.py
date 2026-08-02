"""PostgreSQL 16 integration coverage for the destructive Alembic baseline."""

import asyncio
import os
import uuid
from collections.abc import Iterator
from pathlib import Path

import asyncpg
import pytest
from alembic.command import upgrade as alembic_upgrade
from alembic.config import Config as AlembicConfig
from alembic.operations import Operations

from app.core.config import get_settings

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ALEMBIC_INI = PROJECT_ROOT / "alembic.ini"
ALEMBIC_DIR = PROJECT_ROOT / "alembic"


def _postgres_test_settings() -> tuple[str, str, int, str, str]:
    """Read opt-in PostgreSQL 16 test connection settings."""
    host = os.getenv("POSTGRES_TEST_HOST")
    if host is None:
        pytest.skip("POSTGRES_TEST_HOST is required for PostgreSQL Alembic integration tests")

    return (
        host,
        os.getenv("POSTGRES_TEST_USER", "finhealth"),
        int(os.getenv("POSTGRES_TEST_PORT", "5432")),
        os.getenv("POSTGRES_TEST_PASSWORD", "secret"),
        os.getenv("POSTGRES_TEST_ADMIN_DB", "postgres"),
    )


async def _create_database(
    host: str, user: str, port: int, password: str, admin_database: str, database: str
) -> None:
    connection = await asyncpg.connect(
        host=host, user=user, port=port, password=password, database=admin_database
    )
    try:
        await connection.execute(f'CREATE DATABASE "{database}"')
    finally:
        await connection.close()


async def _drop_database(
    host: str, user: str, port: int, password: str, admin_database: str, database: str
) -> None:
    connection = await asyncpg.connect(
        host=host, user=user, port=port, password=password, database=admin_database
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
def postgres_database(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[str, str, int, str, str]]:
    """Provide an isolated disposable database without changing global test fixtures."""
    host, user, port, password, admin_database = _postgres_test_settings()
    database = f"finhealth_wu2_{uuid.uuid4().hex}"
    asyncio.run(_create_database(host, user, port, password, admin_database, database))
    monkeypatch.setenv("POSTGRES_HOST", host)
    monkeypatch.setenv("POSTGRES_USER", user)
    monkeypatch.setenv("POSTGRES_PORT", str(port))
    monkeypatch.setenv("POSTGRES_PASSWORD", password)
    monkeypatch.setenv("POSTGRES_DB", database)
    get_settings.cache_clear()
    try:
        yield host, user, port, password, database
    finally:
        get_settings.cache_clear()
        asyncio.run(_drop_database(host, user, port, password, admin_database, database))


def _alembic_config() -> AlembicConfig:
    config = AlembicConfig(str(ALEMBIC_INI))
    config.set_main_option("script_location", str(ALEMBIC_DIR))
    return config


def _run(coroutine: object) -> object:
    return asyncio.run(coroutine)  # type: ignore[arg-type]


def test_preseeded_database_fails_before_baseline_ddl(
    postgres_database: tuple[str, str, int, str, str],
) -> None:
    """A legacy user table must reject the destructive baseline without partial DDL."""
    host, user, port, password, database = postgres_database

    async def seed_and_verify() -> None:
        connection = await asyncpg.connect(
            host=host, user=user, port=port, password=password, database=database
        )
        try:
            await connection.execute("CREATE TABLE legacy_data (id integer PRIMARY KEY)")
        finally:
            await connection.close()

    _run(seed_and_verify())
    with pytest.raises(RuntimeError, match="non-empty database"):
        alembic_upgrade(_alembic_config(), "head")

    async def assert_no_partial_schema() -> None:
        connection = await asyncpg.connect(
            host=host, user=user, port=port, password=password, database=database
        )
        try:
            assert (
                await connection.fetchval("SELECT to_regclass('public.legacy_data')")
                == "legacy_data"
            )
            assert await connection.fetchval("SELECT to_regclass('public.banks')") is None
        finally:
            await connection.close()

    _run(assert_no_partial_schema())


def test_empty_database_creates_schema_constraints_and_deterministic_seeds(
    postgres_database: tuple[str, str, int, str, str],
) -> None:
    """The sole baseline creates the current schema and stable reference rows."""
    host, user, port, password, database = postgres_database
    alembic_upgrade(_alembic_config(), "head")

    async def assert_schema() -> None:
        connection = await asyncpg.connect(
            host=host, user=user, port=port, password=password, database=database
        )
        try:
            tables = {
                record["relname"]
                for record in await connection.fetch(
                    "SELECT relname FROM pg_class WHERE relkind = 'r' "
                    "AND relnamespace = 'public'::regnamespace"
                )
            }
            assert {
                "banks",
                "categories",
                "credit_cards",
                "statements",
                "transactions",
                "merchants",
                "merchant_aliases",
                "recurring_rules",
            } <= tables
            assert await connection.fetchval("SELECT count(*) FROM banks") == 3
            assert await connection.fetchval("SELECT count(*) FROM categories") == 12
            assert (
                await connection.fetchval(
                    "SELECT count(*) FROM pg_constraint WHERE conname = 'uq_recurring_rules_upsert_key'"
                )
                == 1
            )
            assert (
                await connection.fetchval(
                    "SELECT count(*) FROM pg_indexes WHERE indexname = 'ix_transactions_recurring_rule_id'"
                )
                == 1
            )
        finally:
            await connection.close()

    _run(assert_schema())


def test_mid_baseline_failure_rolls_back_schema(
    postgres_database: tuple[str, str, int, str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A seed failure cannot leave baseline DDL or an Alembic stamp behind."""
    host, user, port, password, database = postgres_database

    def fail_seed(self: Operations, *args: object, **kwargs: object) -> None:
        raise RuntimeError("injected baseline seed failure")

    monkeypatch.setattr(Operations, "bulk_insert", fail_seed)
    with pytest.raises(RuntimeError, match="injected baseline seed failure"):
        alembic_upgrade(_alembic_config(), "head")

    async def assert_rollback() -> None:
        connection = await asyncpg.connect(
            host=host, user=user, port=port, password=password, database=database
        )
        try:
            assert await connection.fetchval("SELECT to_regclass('public.banks')") is None
            assert await connection.fetchval("SELECT to_regclass('public.alembic_version')") is None
        finally:
            await connection.close()

    _run(assert_rollback())


def test_only_postgresql_baseline_revision_exists() -> None:
    """The undeployed SQLite-oriented migration lineage is not retained."""
    versions = ALEMBIC_DIR / "versions"
    assert [path.name for path in versions.glob("*.py")] == ["0001_postgresql_baseline.py"]
