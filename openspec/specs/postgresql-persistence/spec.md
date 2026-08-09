# PostgreSQL Persistence Specification

## Purpose
PostgreSQL is the sole persistence contract. The system MUST NOT contain SQLite runtime, fallback, import/backup/restore, dependencies, documentation, or dual-dialect tests. Application and domain behavior remain unchanged.

**Hard apply precondition:** `authority_corrupted` MUST be explicitly resolved (superseded or restored by maintainer) before apply.

## Requirements

### Requirement: PostgreSQL-Only Configuration

The system MUST construct the `postgresql+asyncpg` URL via `sqlalchemy.engine.URL.create` from raw structured `POSTGRES_*` settings. Callers MUST pass credential values to `URL.create` verbatim — no caller-side percent-encoding, no manual URL concatenation, no Compose-side URL assembly. The URL object MUST be passed directly to the engine. Rendered wire URLs MAY percent-escape reserved characters per URL syntax. Logs MUST redact credentials (`render_as_string(hide_password=True)`). `DATABASE_URL` MUST NOT be read.

| Field | Required | Missing / Empty / Default Semantics |
|---|---|---|
| `POSTGRES_USER` | yes | missing/empty → fail fast before engine creation |
| `POSTGRES_PASSWORD` | yes | missing/empty → fail fast before engine creation |
| `POSTGRES_HOST` | yes | default `postgres` when unset |
| `POSTGRES_PORT` | yes | default `5432` when unset |
| `POSTGRES_DB` | yes | missing/empty → fail fast before engine creation |

#### Scenario: Startup builds URL object from raw settings

- GIVEN structured `POSTGRES_*` fields are set
- WHEN the application starts
- THEN `URL.create("postgresql+asyncpg", ...)` receives raw values without caller-side encoding
- AND the URL object is passed directly to the engine
- AND no `DATABASE_URL` is read

#### Scenario: Reserved characters are handled by URL.create, not the caller

- GIVEN `POSTGRES_PASSWORD=p@ss:w0rd/foo#bar`
- WHEN the URL object renders to a wire string
- THEN the wire URL MAY percent-escape reserved characters per URL syntax
- AND no caller-side or Compose-side percent-encoding was performed
- AND SQLAlchemy receives the original raw password at connect time

#### Scenario: Missing or empty required field fails fast

- GIVEN any of `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB` is unset or empty
- WHEN configuration validates
- THEN the process exits non-zero identifying the missing field
- AND no engine is created

#### Scenario: Credentials are redacted in logs

- GIVEN a valid configuration
- WHEN the URL is logged
- THEN the password is masked
- AND raw credentials never appear in log output

### Requirement: No SQLite Runtime or Fallback

The system MUST NOT contain SQLite dialect detection, WAL pragma hooks, `aiosqlite`, SQLite engine options, SQLite path creation, or branches on SQLite vs PostgreSQL. `create_engine` MUST assume PostgreSQL unconditionally.

#### Scenario: No SQLite imports

- GIVEN the source tree
- WHEN grepped for `sqlite` or `aiosqlite`
- THEN zero matches exist outside removal documentation

### Requirement: No SQLite CLI Operations

The system MUST NOT provide any SQLite CLI subcommand. Documentation MUST NOT reference SQLite import/backup workflows.

#### Scenario: SQLite CLI is absent

- GIVEN the installed CLI
- WHEN `--help` runs
- THEN no SQLite subcommands appear

### Requirement: Destructive Empty-Database Baseline

Alembic MUST contain exactly one baseline migration. The baseline MUST preflight database emptiness before any DDL and MUST fail with a clear error if user tables exist — no partial schema changes may be applied. Historical migrations MUST NOT exist. The baseline MUST create all tables, constraints, indexes, and seed data on an empty database.

#### Scenario: Empty-volume migration succeeds

- GIVEN a fresh PostgreSQL volume
- WHEN `alembic upgrade head` runs
- THEN tables, constraints, and seeds are created
- AND the application starts

#### Scenario: Non-empty database fails preflight

- GIVEN existing user tables
- WHEN `alembic upgrade head` runs
- THEN the migration fails before any DDL with a clear "non-empty database" error
- AND no partial schema changes are applied

#### Scenario: Seeds populate reference data

- GIVEN an empty database
- WHEN baseline completes
- THEN required seeds (e.g., `banks`) are present

### Requirement: PostgreSQL Test Isolation

Tests MUST use PostgreSQL exclusively, each against an isolated database or schema created before and dropped after the test. Fixtures MUST use the same structured `POSTGRES_*` fields as production.

#### Scenario: Test isolation

- GIVEN a test run
- WHEN a fixture is created
- THEN a disposable PostgreSQL database/schema is provisioned
- AND it is dropped after the test regardless of outcome

#### Scenario: No dual-dialect tests

- GIVEN the test tree
- WHEN inspected
- THEN no test branches on dialect or uses SQLite

### Requirement: Compose-First Deployment with Single Migration Owner

Compose MUST declare PostgreSQL with a named volume, depend on `postgres` healthcheck, pass `POSTGRES_*` structured fields, and MUST NOT set `DATABASE_URL`. The design MUST designate exactly one Compose migration owner and one ordered migration step — no two services or files may independently run migrations. The `./data` SQLite bind mount MUST be removed.

#### Scenario: Compose startup

- GIVEN `docker compose up -d`
- WHEN postgres is healthy
- THEN the designated owner runs `alembic upgrade head` exactly once
- AND `finhealth` starts and connects

#### Scenario: Compose passes structured fields

- GIVEN `.env` with `POSTGRES_*`
- WHEN `docker compose config` runs
- THEN all five structured fields are passed
- AND no `DATABASE_URL` is set

#### Scenario: Data persists across restarts

- GIVEN data is written
- WHEN `finhealth` restarts
- THEN data remains accessible

### Requirement: Operations Runbook

Documentation MUST provide a single lifecycle runbook: initial deployment (empty volume), migration, backup (`pg_dump`), restore (`pg_restore`), and destructive reset (volume deletion). SQLite procedures MUST NOT be referenced.

#### Scenario: Runbook covers lifecycle

- GIVEN an operator follows the runbook
- WHEN steps execute
- THEN deployment, backup, restore, and destructive reset succeed with PostgreSQL-native tools

### Requirement: Dependency and Documentation Cleanup

`pyproject.toml` MUST NOT list `aiosqlite`. `README.md` and `.env.example` MUST NOT reference SQLite as a deployment option. Documentation MUST state PostgreSQL as the sole supported database.

#### Scenario: No SQLite dependency

- GIVEN dependencies
- WHEN inspected
- THEN `aiosqlite` is absent

#### Scenario: Documentation states PostgreSQL-only

- GIVEN docs
- WHEN inspected
- THEN no SQLite-as-supported mention exists
