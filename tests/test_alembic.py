"""PostgreSQL 16 integration coverage for the destructive Alembic baseline."""

import asyncio
import os
import uuid
from collections.abc import Iterator
from pathlib import Path

import asyncpg
import pytest
from alembic.command import downgrade as alembic_downgrade
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
            assert await connection.fetchval("SELECT count(*) FROM categories") == 13
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


def test_baseline_and_source_revisions_exist() -> None:
    """The exact migration lineage is 0001 (destructive) → 0002 → 0003."""
    versions = ALEMBIC_DIR / "versions"
    assert sorted(path.name for path in versions.glob("*.py")) == [
        "0001_postgresql_baseline.py",
        "0002_statement_source.py",
        "0003_card_payments_category.py",
    ]

    from alembic.script import ScriptDirectory

    script = ScriptDirectory.from_config(_alembic_config())
    assert script.get_current_head() == "0003_card_payments_category"
    baseline = script.get_revision("0001_postgresql_baseline")
    assert baseline.down_revision is None
    source_revision = script.get_revision("0002_statement_source")
    assert source_revision.down_revision == "0001_postgresql_baseline"
    card_payments_revision = script.get_revision("0003_card_payments_category")
    assert card_payments_revision.down_revision == "0002_statement_source"


# ---------------------------------------------------------------------------
# 0002_statement_source: forward/backward traversal on populated databases
# ---------------------------------------------------------------------------


async def _connect(
    host: str, user: str, port: int, password: str, database: str
) -> asyncpg.Connection:
    return await asyncpg.connect(
        host=host, user=user, port=port, password=password, database=database
    )


def _seed_populated_baseline(
    host: str,
    user: str,
    port: int,
    password: str,
    database: str,
) -> tuple[str, str, str, str, str, str]:
    """Upgrade an empty database to baseline and insert two PDF-era statements.

    Returns ``(file_path_a, file_hash_a, file_path_b, file_hash_b,
    card_a_id, card_b_id)`` so tests can assert preservation. The two
    statements use *different* cards with the same hash to mirror the
    historical cross-card allowance.
    """
    alembic_upgrade(_alembic_config(), "0001_postgresql_baseline")

    async def _seed() -> tuple[str, str, str, str, str, str]:
        connection = await _connect(host, user, port, password, database)
        try:
            card_a = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
            card_b = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
            statement_a_id = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
            statement_b_id = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
            file_path_a = "santander/2026-05.pdf"
            file_hash_a = "a" * 64
            file_path_b = "itau/2026-05.pdf"
            file_hash_b = "a" * 64  # same hash, different card: allowed
            await connection.execute(
                """
                INSERT INTO credit_cards (id, created_at, updated_at, bank_id,
                    card_number_masked, cardholder, currency, is_active)
                VALUES ($1, now(), now(), '00000000-0000-0000-0000-000000000001',
                        'X1', 'A', 'CLP', true),
                       ($2, now(), now(), '00000000-0000-0000-0000-000000000001',
                        'X2', 'B', 'USD', true)
                """,
                card_a,
                card_b,
            )
            await connection.execute(
                """
                INSERT INTO statements (id, created_at, updated_at, credit_card_id,
                    period_start, period_end, statement_date, file_path, file_hash, status)
                VALUES ($1, now(), now(), $3, '2026-05-01', '2026-05-31', '2026-06-01',
                        $5, $6, 'completed'),
                       ($2, now(), now(), $4, '2026-05-01', '2026-05-31', '2026-06-01',
                        $7, $6, 'failed')
                """,
                statement_a_id,
                statement_b_id,
                card_a,
                card_b,
                file_path_a,
                file_hash_a,
                file_path_b,
            )
            return file_path_a, file_hash_a, file_path_b, file_hash_b, card_a, card_b
        finally:
            await connection.close()

    return _run(_seed())


def test_upgrade_backfills_source_pdf_and_preserves_files(
    postgres_database: tuple[str, str, int, str, str],
) -> None:
    """Populated baseline → head backfills ``source='pdf'`` and keeps file data."""
    host, user, port, password, database = postgres_database
    file_path_a, file_hash_a, file_path_b, file_hash_b, _card_a, _card_b = _seed_populated_baseline(
        host, user, port, password, database
    )

    alembic_upgrade(_alembic_config(), "head")

    async def assert_upgraded() -> None:
        connection = await _connect(host, user, port, password, database)
        try:
            rows = await connection.fetch(
                "SELECT id, credit_card_id, file_path, file_hash, status, source "
                "FROM statements ORDER BY id"
            )
            assert len(rows) == 2
            by_id = {row["id"]: row for row in rows}
            row_a = by_id["cccccccc-cccc-4ccc-8ccc-cccccccccccc"]
            row_b = by_id["dddddddd-dddd-4ddd-8ddd-dddddddddddd"]
            # File data preserved verbatim, statuses untouched.
            assert row_a["file_path"] == file_path_a
            assert row_a["file_hash"] == file_hash_a
            assert row_a["status"] == "completed"
            assert row_b["file_path"] == file_path_b
            assert row_b["file_hash"] == file_hash_b
            assert row_b["status"] == "failed"
            # Every historical row is backfilled as a PDF statement.
            assert row_a["source"] == "pdf"
            assert row_b["source"] == "pdf"
            # File NOT NULL constraints are dropped.
            nullability = await connection.fetch(
                "SELECT column_name, is_nullable FROM information_schema.columns "
                "WHERE table_name = 'statements' AND column_name IN "
                "('file_path', 'file_hash', 'source')"
            )
            nullable = {row["column_name"]: row["is_nullable"] for row in nullability}
            assert nullable["file_path"] == "YES"
            assert nullable["file_hash"] == "YES"
            assert nullable["source"] == "NO"
            # Server default keeps old PDF writers working.
            default = await connection.fetchval(
                "SELECT column_default FROM information_schema.columns "
                "WHERE table_name = 'statements' AND column_name = 'source'"
            )
            assert default is not None and "pdf" in default
        finally:
            await connection.close()

    _run(assert_upgraded())


def test_head_schema_behaviour_for_api_rows_and_source_validation(
    postgres_database: tuple[str, str, int, str, str],
) -> None:
    """At head: API rows with null files store freely; source validation holds."""
    host, user, port, password, database = postgres_database
    _seed_populated_baseline(host, user, port, password, database)
    alembic_upgrade(_alembic_config(), "head")

    async def assert_behaviour() -> None:
        connection = await _connect(host, user, port, password, database)
        try:
            check_count = await connection.fetchval(
                "SELECT count(*) FROM pg_constraint WHERE conname = 'ck_statements_source' "
                "AND conrelid = 'statements'::regclass"
            )
            assert check_count == 1
            # Two API statements on the same card, both with null files: ordinary
            # nullable unique semantics must allow multiple null hashes.
            await connection.execute(
                """
                INSERT INTO statements (id, created_at, updated_at, credit_card_id,
                    period_start, period_end, statement_date, file_path, file_hash,
                    status, source)
                VALUES ($1, now(), now(), $3, '2026-09-01', '2026-09-30', '2026-10-01',
                        NULL, NULL, 'completed', 'api'),
                       ($2, now(), now(), $3, '2026-10-01', '2026-10-31', '2026-11-01',
                        NULL, NULL, 'completed', 'api')
                """,
                "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee",
                "ffffffff-ffff-4fff-8fff-ffffffffffff",
                "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            )
            api_rows = await connection.fetch(
                "SELECT source, file_path, file_hash FROM statements WHERE source = 'api'"
            )
            assert len(api_rows) == 2
            assert all(row["file_path"] is None and row["file_hash"] is None for row in api_rows)
            # Same non-null hash, same card → rejected; cross-card → still allowed.
            constraint_count = await connection.fetchval(
                "SELECT count(*) FROM pg_constraint "
                "WHERE conname = 'uq_statements_credit_card_id_file_hash' "
                "AND conrelid = 'statements'::regclass"
            )
            assert constraint_count == 1
            with pytest.raises(asyncpg.UniqueViolationError):
                await connection.execute(
                    """
                    INSERT INTO statements (id, created_at, updated_at, credit_card_id,
                        period_start, period_end, statement_date, file_path, file_hash,
                        status, source)
                    VALUES ($1, now(), now(), $2, '2026-06-01', '2026-06-30', '2026-07-01',
                            'other.pdf', $3, 'completed', 'pdf')
                    """,
                    "11111111-1111-4111-8111-111111111111",
                    "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                    "a" * 64,
                )
            assert (
                await connection.fetchval(
                    "SELECT count(*) FROM statements WHERE file_hash = $1", "a" * 64
                )
                == 2
            )
            base = (
                "INSERT INTO statements (id, created_at, updated_at, credit_card_id, "
                "period_start, period_end, statement_date, file_path, file_hash, "
                "status, source) VALUES ($1, now(), now(), $2, '2026-09-01', "
                "'2026-09-30', '2026-10-01', 'p.pdf', $3, 'completed', $4)"
            )
            with pytest.raises(asyncpg.CheckViolationError):
                await connection.execute(
                    base,
                    "22222222-2222-4333-8444-555555555555",
                    "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                    "b" * 64,
                    "web",
                )
            with pytest.raises(asyncpg.NotNullViolationError):
                await connection.execute(
                    base.replace("'completed', $4)", "'completed', NULL)"),
                    "33333333-3333-4333-8444-555555555555",
                    "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                    "c" * 64,
                )
        finally:
            await connection.close()

    _run(assert_behaviour())


def test_downgrade_to_baseline_refuses_api_rows(
    postgres_database: tuple[str, str, int, str, str],
) -> None:
    """Downgrade with an API/null-file row must fail before any DDL or stamp change."""
    host, user, port, password, database = postgres_database
    file_path_a, file_hash_a, _file_path_b, _file_hash_b, _card_a, _card_b = (
        _seed_populated_baseline(host, user, port, password, database)
    )
    alembic_upgrade(_alembic_config(), "head")

    async def insert_api_row() -> None:
        connection = await _connect(host, user, port, password, database)
        try:
            await connection.execute(
                """
                INSERT INTO statements (id, created_at, updated_at, credit_card_id,
                    period_start, period_end, statement_date, file_path, file_hash,
                    status, source)
                VALUES ($1, now(), now(), $2, '2026-09-01', '2026-09-30', '2026-10-01',
                        NULL, NULL, 'completed', 'api')
                """,
                "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee",
                "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            )
        finally:
            await connection.close()

    _run(insert_api_row())
    with pytest.raises(RuntimeError, match="incompatible statements"):
        alembic_downgrade(_alembic_config(), "0001_postgresql_baseline")

    async def assert_unchanged() -> None:
        connection = await _connect(host, user, port, password, database)
        try:
            # Refusal leaves the newest schema, the revision stamp and all data intact.
            revision = await connection.fetchval("SELECT version_num FROM alembic_version")
            assert revision == "0003_card_payments_category"
            nullable = await connection.fetchval(
                "SELECT is_nullable FROM information_schema.columns "
                "WHERE table_name = 'statements' AND column_name = 'file_path'"
            )
            assert nullable == "YES"
            api_row = await connection.fetchval(
                "SELECT count(*) FROM statements WHERE source = 'api'"
            )
            assert api_row == 1
            pdf_rows = await connection.fetch(
                "SELECT file_path, file_hash, source FROM statements WHERE source = 'pdf'"
            )
            assert pdf_rows[0]["file_path"] == file_path_a
            assert pdf_rows[0]["file_hash"] == file_hash_a
        finally:
            await connection.close()

    _run(assert_unchanged())


def test_safe_downgrade_restores_not_null_and_upgrades_again(
    postgres_database: tuple[str, str, int, str, str],
) -> None:
    """Without API rows, downgrade restores NOT NULL and the upgrade round-trips."""
    host, user, port, password, database = postgres_database
    file_path_a, file_hash_a, _file_path_b, _file_hash_b, _card_a, _card_b = (
        _seed_populated_baseline(host, user, port, password, database)
    )
    alembic_upgrade(_alembic_config(), "head")
    alembic_downgrade(_alembic_config(), "0001_postgresql_baseline")

    async def assert_downgraded() -> None:
        connection = await _connect(host, user, port, password, database)
        try:
            revision = await connection.fetchval("SELECT version_num FROM alembic_version")
            assert revision == "0001_postgresql_baseline"
            for column in ("file_path", "file_hash"):
                nullable = await connection.fetchval(
                    "SELECT is_nullable FROM information_schema.columns "
                    "WHERE table_name = 'statements' AND column_name = $1",
                    column,
                )
                assert nullable == "NO"
            source_exists = await connection.fetchval(
                "SELECT count(*) FROM information_schema.columns "
                "WHERE table_name = 'statements' AND column_name = 'source'"
            )
            assert source_exists == 0
            rows = await connection.fetch(
                "SELECT file_path, file_hash, status FROM statements ORDER BY id"
            )
            assert rows[0]["file_path"] == file_path_a
            assert rows[0]["file_hash"] == file_hash_a
            assert rows[0]["status"] == "completed"
        finally:
            await connection.close()

    _run(assert_downgraded())
    # The round-trip must leave a database the rest of the chain can still upgrade.
    alembic_upgrade(_alembic_config(), "head")


def test_migration_runner_traverses_versioned_populated_database(
    postgres_database: tuple[str, str, int, str, str],
) -> None:
    """The runner must allow forward traversal on a recognized versioned database."""
    host, user, port, password, database = postgres_database
    _seed_populated_baseline(host, user, port, password, database)
    # A versioned populated database is no longer refused: 0002 and 0003 apply cleanly.
    alembic_upgrade(_alembic_config(), "head")
    revision = _revision_of(host, user, port, password, database)
    assert revision == "0003_card_payments_category"


def _revision_of(host: str, user: str, port: int, password: str, database: str) -> str:
    async def _query() -> str:
        connection = await _connect(host, user, port, password, database)
        try:
            revision: str = await connection.fetchval("SELECT version_num FROM alembic_version")
            return revision
        finally:
            await connection.close()

    return _run(_query())


def test_head_no_op_and_unknown_revision_failures(
    postgres_database: tuple[str, str, int, str, str],
) -> None:
    """Upgrading at head is a no-op; unknown revisions fail without data loss."""
    host, user, port, password, database = postgres_database
    _seed_populated_baseline(host, user, port, password, database)
    alembic_upgrade(_alembic_config(), "head")
    # At-head upgrade is a no-op (idempotent).
    alembic_upgrade(_alembic_config(), "head")
    revision = _revision_of(host, user, port, password, database)
    assert revision == "0003_card_payments_category"
    with pytest.raises(Exception):  # noqa: B017 - alembic raises various revision errors
        alembic_upgrade(_alembic_config(), "0009_does_not_exist")
    revision = _revision_of(host, user, port, password, database)
    assert revision == "0003_card_payments_category"


def test_offline_downgrade_sql_guard_coverage(
    postgres_database: tuple[str, str, int, str, str],
) -> None:
    """The offline downgrade SQL embeds the same lock/guard as the online path."""
    host, user, port, password, database = postgres_database
    _seed_populated_baseline(host, user, port, password, database)
    alembic_upgrade(_alembic_config(), "head")

    import contextlib
    from io import StringIO

    buffer = StringIO()
    with contextlib.redirect_stdout(buffer):
        alembic_downgrade(
            _alembic_config(),
            "0002_statement_source:0001_postgresql_baseline",
            sql=True,
        )
    emitted = buffer.getvalue()
    # The offline script must lock the statements table and run the refusal
    # guard before any ALTER touches the schema.
    assert "LOCK TABLE statements" in emitted
    assert "file_path IS NULL" in emitted
    assert "file_hash IS NULL" in emitted
    assert "source" in emitted
    guard_position = emitted.index("file_path IS NULL")
    first_alter = emitted.index("ALTER TABLE")
    assert guard_position < first_alter


# ---------------------------------------------------------------------------
# 0003_card_payments_category: idempotent additive category seed
# ---------------------------------------------------------------------------


_CARD_PAYMENTS_ID = "10000000-0000-0000-0000-000000000013"
_CARD_PAYMENTS_NAME = "Card Payments"


async def _categories_count(host: str, user: str, port: int, password: str, database: str) -> int:
    connection = await _connect(host, user, port, password, database)
    try:
        count: int = await connection.fetchval("SELECT count(*) FROM categories")
        return count
    finally:
        await connection.close()


async def _categories_rows(host: str, user: str, port: int, password: str, database: str) -> list:
    connection = await _connect(host, user, port, password, database)
    try:
        return await connection.fetch(
            "SELECT id, name, display_name, sort_order FROM categories ORDER BY sort_order"
        )
    finally:
        await connection.close()


def test_upgrade_to_head_seeds_card_payments_category(
    postgres_database: tuple[str, str, int, str, str],
) -> None:
    """Upgrade to head inserts the 13th closed-set category deterministically."""
    host, user, port, password, database = postgres_database
    alembic_upgrade(_alembic_config(), "head")

    async def assert_seeded() -> None:
        rows = await _categories_rows(host, user, port, password, database)
        assert len(rows) == 13
        card_payments = rows[-1]
        assert card_payments["id"] == _CARD_PAYMENTS_ID
        assert card_payments["name"] == _CARD_PAYMENTS_NAME
        assert card_payments["display_name"] == _CARD_PAYMENTS_NAME
        assert card_payments["sort_order"] == 13

    _run(assert_seeded())


def test_downgrade_0003_restores_twelve_and_reupgrades(
    postgres_database: tuple[str, str, int, str, str],
) -> None:
    """Downgrading 0003 deletes the Card Payments row; the upgrade re-adds it."""
    host, user, port, password, database = postgres_database
    alembic_upgrade(_alembic_config(), "head")
    alembic_downgrade(_alembic_config(), "0002_statement_source")

    async def assert_downgraded() -> None:
        assert await _categories_count(host, user, port, password, database) == 12
        rows = await _categories_rows(host, user, port, password, database)
        assert all(row["name"] != _CARD_PAYMENTS_NAME for row in rows)

    _run(assert_downgraded())

    # The round-trip must leave the chain upgradeable again.
    alembic_upgrade(_alembic_config(), "head")

    async def assert_reseeded() -> None:
        rows = await _categories_rows(host, user, port, password, database)
        assert len(rows) == 13
        card_payments = rows[-1]
        assert card_payments["id"] == _CARD_PAYMENTS_ID
        assert card_payments["name"] == _CARD_PAYMENTS_NAME
        assert card_payments["display_name"] == _CARD_PAYMENTS_NAME
        assert card_payments["sort_order"] == 13

    _run(assert_reseeded())


def test_card_payments_seed_is_idempotent_on_same_id(
    postgres_database: tuple[str, str, int, str, str],
) -> None:
    """An already-seeded Card Payments row (same id) is never duplicated."""
    host, user, port, password, database = postgres_database
    alembic_upgrade(_alembic_config(), "0002_statement_source")

    async def preseed() -> None:
        connection = await _connect(host, user, port, password, database)
        try:
            await connection.execute(
                "INSERT INTO categories (id, created_at, updated_at, name, "
                "display_name, sort_order) "
                "VALUES ($1, now(), now(), $2, $3, 13)",
                _CARD_PAYMENTS_ID,
                _CARD_PAYMENTS_NAME,
                _CARD_PAYMENTS_NAME,
            )
        finally:
            await connection.close()

    _run(preseed())
    alembic_upgrade(_alembic_config(), "head")

    _run(assert_thirteen(host, user, port, password, database))


def test_card_payments_seed_is_idempotent_on_same_name(
    postgres_database: tuple[str, str, int, str, str],
) -> None:
    """A pre-existing category row with the same name but a different id survives.

    The upgrade must neither fail on the unique ``name`` constraint nor
    insert a second row: the operator's own row is adopted as-is.
    """
    host, user, port, password, database = postgres_database
    alembic_upgrade(_alembic_config(), "0002_statement_source")
    operator_id = "99999999-9999-9999-9999-999999999999"

    async def preseed() -> None:
        connection = await _connect(host, user, port, password, database)
        try:
            await connection.execute(
                "INSERT INTO categories (id, created_at, updated_at, name, "
                "display_name, sort_order) "
                "VALUES ($1, now(), now(), $2, $3, 99)",
                operator_id,
                _CARD_PAYMENTS_NAME,
                _CARD_PAYMENTS_NAME,
            )
        finally:
            await connection.close()

    _run(preseed())
    alembic_upgrade(_alembic_config(), "head")

    async def assert_no_duplicate() -> None:
        rows = await _categories_rows(host, user, port, password, database)
        card_payments_rows = [row for row in rows if row["name"] == _CARD_PAYMENTS_NAME]
        assert len(card_payments_rows) == 1
        # The operator's own row identity is preserved; no deterministic
        # duplicate was inserted under the migration id.
        assert card_payments_rows[0]["id"] == operator_id
        assert len(rows) == 13

    _run(assert_no_duplicate())


async def assert_thirteen(host: str, user: str, port: int, password: str, database: str) -> None:
    """Shared assertion: exactly 13 categories, Card Payments present once."""
    rows = await _categories_rows(host, user, port, password, database)
    assert len(rows) == 13
    card_payments_rows = [row for row in rows if row["name"] == _CARD_PAYMENTS_NAME]
    assert len(card_payments_rows) == 1
    assert card_payments_rows[0]["id"] == _CARD_PAYMENTS_ID
