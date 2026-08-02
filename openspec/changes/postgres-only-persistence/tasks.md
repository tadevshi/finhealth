# Tasks: PostgreSQL-Only Persistence

> **Hard authority gate (blocking prerequisite).** `sdd-apply` MUST NOT start
> until a maintainer explicitly resolves `authority_corrupted` (restored or
> superseded). Recorded in `spec.md`, `design.md`, and again as item `0.0`
> in every phase. Do not work around it.

## Review Workload Forecast

Decision needed before apply: Yes
Chained PRs recommended: Yes
Chain strategy: stacked-to-main
400-line budget risk: High
Estimated changed lines: 1200–1800; 4 stacked PRs.

### Work Units

| Unit | Goal | Focused test | Runtime harness | Rollback |
|------|------|--------------|-----------------|----------|
| WU-1 | `POSTGRES_*` + URL-object engine + redacted logs | `pytest tests/test_config.py tests/test_db.py -k postgres_only --no-cov` | `uvicorn app.main:app` boots; `render_as_string(hide_password=True)` masks | Revert `app/core/{config,lifespan}.py`, `app/db/{engine,session}.py`, `alembic/env.py`, `pyproject.toml`, and `tests/test_config.py` |
| WU-2 | One baseline + preflight + delete 8 historical migrations | `pytest tests/test_alembic.py tests/test_postgres_integration.py --no-cov` | `docker compose run --rm migrate` empty = OK; pre-seeded = non-zero before DDL | Revert `alembic/env.py`; delete `0001_postgresql_baseline.py`; restore 8 `000x_*.py` |
| WU-3 | PG fixtures + dep cleanup + delete SQLite CLI/tests | `pytest tests/ --no-cov -m "not docker_lifecycle"` | `docker compose config` = `POSTGRES_*` only; `rg "aiosqlite|sqlite_ops|import_sqlite" app tests pyproject.toml` = 0 | Restore `app/cli/{import_sqlite,sqlite_ops}.py`, related tests; revert `pyproject.toml`, lockfile, `tests/conftest.py` |
| WU-4 | Single Compose migration owner + `.env.example` + README + Dockerfile | `bash scripts/verify.sh`; `docker compose config` | `up -d`: `postgres` healthy → `migrate` once → `finhealth`; `pytest tests/test_docker_lifecycle.py --no-cov` | Revert `docker-compose*.yml`, `Dockerfile`, `.env.example`, `README.md`; recreate `postgres_data` |

## Phase 1: Configuration & Engine Boundary (WU-1)

- [x] **0.0 BLOCKING PREREQUISITE — `authority_corrupted` resolved (maintainer).** Continuation authorized by the maintainer's explicit native-attempt reset.
- [x] 1.1 RED `tests/test_config.py`: `Settings` fails fast when `POSTGRES_USER`/`PASSWORD`/`DB` missing or empty, before engine creation.
- [x] 1.2 RED: `POSTGRES_HOST` defaults `postgres` only when unset, rejects `""`; `POSTGRES_PORT` defaults `5432`, rejects non-int and out-of-range.
- [x] 1.3 RED: `Settings.database_url` returns `URL` with `drivername="postgresql+asyncpg"`; reserved chars preserved; `render_as_string(hide_password=True)` masks.
- [x] 1.4 RED: no `DATABASE_URL` field on `Settings`; no code path reads it.
- [x] 1.5 GREEN rewrite `app/core/config.py` — replace `DATABASE_URL` with five `POSTGRES_*`; add `database_url` via `URL.create("postgresql+asyncpg", ...)`.
- [x] 1.6 GREEN rewrite `app/db/engine.py` — accept URL object directly; drop `_enable_sqlite_wal`, `PRAGMA journal_mode=WAL`, `aiosqlite` branches, dialect detection.
- [x] 1.7 GREEN update `app/db/session.py` and `app/core/lifespan.py` to consume `settings.database_url`; log with `render_as_string(hide_password=True)` only.
- [x] 1.8 REFACTOR drop `unquote`/`urlparse` SQLite path; every `create_engine` caller passes the URL object.
- [x] 1.9 GREEN add `asyncpg` to the runtime dependencies so the WU-1 `postgresql+asyncpg` startup harness can load its dialect. This dependency is intentionally assigned to WU-1; removing `aiosqlite` remains WU-3.

## Phase 2: Alembic Baseline & Cleanup (WU-2)

- [ ] **0.0 BLOCKING PREREQUISITE** still active.
- [ ] 2.1 RED `tests/test_alembic.py`: `alembic upgrade head` on pre-seeded schema raises non-empty-database error before any DDL.
- [ ] 2.2 RED: on empty database, baseline creates all tables, constraints, indexes, `banks` seeds inside one transaction.
- [ ] 2.3 RED: injected mid-baseline failure rolls the entire transaction back.
- [ ] 2.4 RED: revision files `0001_initial.py`–`0008_phase2_recurring_rules_unique.py` no longer exist.
- [ ] 2.5 GREEN rewrite `alembic/env.py` to use the URL object; add pre-DDL `pg_catalog` preflight (exclude `alembic_version` + system schemas); short-circuit at head.
- [ ] 2.6 GREEN create `alembic/versions/0001_postgresql_baseline.py` — all tables, constraints, indexes, seeds in one transactional `upgrade()`; empty `downgrade()`.
- [ ] 2.7 GREEN delete `0001_initial.py`–`0008_phase2_recurring_rules_unique.py`; clear `__pycache__`.

## Phase 3: Test Isolation & Dependency Cleanup (WU-3)

- [ ] **0.0 BLOCKING PREREQUISITE** still active.
- [ ] 3.1 RED `tests/conftest.py` `test_settings` provisions a disposable PostgreSQL DB/schema before each test and drops it after.
- [ ] 3.2 RED: `rg "aiosqlite|sqlite_ops|import_sqlite" app tests pyproject.toml` = 0 outside removal docs.
- [ ] 3.3 RED: no test branches on dialect.
- [ ] 3.4 GREEN rewrite `tests/conftest.py` to use `POSTGRES_*`; add session admin connection + per-test create/drop helpers.
- [ ] 3.5 GREEN update `tests/test_alembic.py`, `tests/test_postgres_integration.py` for the new baseline; remove SQLite branches.
- [ ] 3.6 GREEN delete `app/cli/import_sqlite.py`, `app/cli/sqlite_ops.py`, `tests/test_sqlite_import.py`, `tests/test_sqlite_ops.py`; strip SQLite asserts in `tests/test_docker_lifecycle.py`.
- [ ] 3.7 GREEN remove `aiosqlite` from `pyproject.toml`; regenerate lockfile; `pip check` clean.

## Phase 4: Compose, Documentation & Operations (WU-4)

- [ ] **0.0 BLOCKING PREREQUISITE** still active.
- [ ] 4.1 RED `docker compose config` — exactly one `migrate` with `command: alembic upgrade head`, `restart: "no"`; `finhealth` depends on `migrate: service_completed_successfully` AND `postgres` healthy.
- [ ] 4.2 RED: merged `docker-compose.yml + docker-compose.self-hosted.yml` defines migration command in exactly one file.
- [ ] 4.3 RED: `docker compose config` lists the five `POSTGRES_*` and no `DATABASE_URL`.
- [ ] 4.4 RED: `Dockerfile` `CMD` = Uvicorn only — no `alembic upgrade` in the image.
- [ ] 4.5 RED: non-zero `migrate` exit blocks `finhealth` from starting.
- [ ] 4.6 GREEN `docker-compose.yml` — add `migrate`, rewire `finhealth` deps, drop `./data` mount, replace `DATABASE_URL` with `POSTGRES_*`.
- [ ] 4.7 GREEN `docker-compose.self-hosted.yml` becomes overlay only.
- [ ] 4.8 GREEN `Dockerfile` runtime `CMD` = Uvicorn only.
- [ ] 4.9 GREEN rewrite `.env.example` — drop `DATABASE_URL`, list five `POSTGRES_*`, remove SQLite comment.
- [ ] 4.10 GREEN rewrite `README.md` — PostgreSQL-only deploy + lifecycle runbook (deploy, migration, `pg_dump`, `pg_restore`, `docker volume rm`).
- [ ] 4.11 GREEN rerun `bash scripts/verify.sh` + lifecycle smoke (start, write, restart, read, `pg_dump`, `pg_restore`).

## Phase 5: Verification & Archive

- [ ] **0.0 BLOCKING PREREQUISITE** — final confirmation `authority_corrupted` resolved before `sdd-archive`.
- [ ] 5.1 Run `sdd-verify`; all spec scenarios pass.
- [ ] 5.2 Confirm `rg "sqlite"` = 0 outside removal notes; `pip check` clean.
- [ ] 5.3 `sdd-archive` → `archive/YYYY-MM-DD-postgres-only-persistence/`; merge delta into `openspec/specs/postgresql-persistence/spec.md`.
