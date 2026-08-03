# Apply Progress: PostgreSQL-Only Persistence

## Work Units

- Delivery: stacked-to-main autonomous slices.
- WU-1 / Phase 1: native attempt generation 1, ordinal 2, passed after the maintainer-authorized reset.
- WU-2 / Phase 2: native attempt generation 3, ordinal 3, passed under a maintainer-approved `size:exception` (3,217 changed lines; approved limit 4,000).

## Implementation

- Replaced `Settings.DATABASE_URL` with validated structured `POSTGRES_*` fields and a raw-value `URL.create("postgresql+asyncpg", ...)` property.
- Changed runtime and Alembic engine construction to consume the SQLAlchemy URL object directly.
- Removed SQLite path creation, WAL hook, dialect detection, and SQLite engine arguments at the engine boundary.
- Added focused configuration/engine-boundary tests for required values, topology validation, raw reserved password handling, URL masking, and direct URL-object engine invocation.
- Added `asyncpg>=0.30,<1.0` to runtime dependencies as the minimal WU-1 dependency correction. Removing `aiosqlite` remains exclusively in WU-3.
- Replaced the historical Alembic lineage with one PostgreSQL baseline containing the current tables, constraints, indexes, deterministic bank/category seeds, and an intentionally empty downgrade.
- Added a PostgreSQL catalog preflight that rejects non-empty user schemas before DDL, excludes system schemas and `alembic_version`, and short-circuits an already-stamped head.
- Added PostgreSQL 16 integration coverage for empty bootstrap, preflight, rollback after injected seed failure, constraints, seeds, and removal of the obsolete revisions.

## Task Status

- [x] 0.0 Authority prerequisite resolved through the maintainer's explicit continuation/reset authorization.
- [x] 1.1–1.4 Configuration RED coverage.
- [x] 1.5–1.7 PostgreSQL URL-object implementation and redacted lifespan logging.
- [x] 1.8 SQLite boundary refactor.
- [x] 1.9 `asyncpg` runtime dependency correction.
- [x] 2.1–2.4 PostgreSQL baseline RED coverage for preflight, bootstrap, rollback, and lineage removal.
- [x] 2.5 PostgreSQL URL-object Alembic runner with pre-DDL catalog preflight.
- [x] 2.6 Transactional `0001_postgresql_baseline` with deterministic seeds and empty downgrade.
- [x] 2.7 Historical `0001`–`0008` migration removal.

## Work Unit Evidence

| Evidence | Exact result |
|---|---|
| Focused test command | `POSTGRES_USER=finhealth POSTGRES_PASSWORD=secret POSTGRES_DB=finhealth python -m pytest tests/test_config.py tests/test_db.py -k postgres_only --no-cov` |
| Focused test result | Exit 0 — 8 passed, 20 deselected in 0.02s. |
| Static checks | `python -m ruff check app/core/config.py app/core/lifespan.py app/db/engine.py app/db/session.py alembic/env.py tests/test_config.py` — exit 0; `python -m ruff format --check ...` — exit 0, 6 files already formatted; `git diff --check` — exit 0. |
| Runtime harness command | Start disposable `postgres:16-alpine` on `127.0.0.1:55432`; then run `timeout --signal=TERM --kill-after=5s 8s env POSTGRES_USER=finhealth POSTGRES_PASSWORD=secret POSTGRES_HOST=127.0.0.1 POSTGRES_PORT=55432 POSTGRES_DB=finhealth python -m uvicorn app.main:app --host 127.0.0.1 --port 18000`. |
| Runtime harness result | Exit 0 after accepting timeout exit 124 as the planned graceful shutdown. Uvicorn logged `Application startup complete`, served on `127.0.0.1:18000`, then completed shutdown cleanly. This proves the `asyncpg` dialect loads and startup reaches PostgreSQL. The disposable container was removed. |
| Rollback boundary | Revert `app/core/config.py`, `app/core/lifespan.py`, `app/db/engine.py`, `app/db/session.py`, `alembic/env.py`, `tests/test_config.py`, and the WU-1 `asyncpg` line in `pyproject.toml`; no WU-2–WU-4 behavior is removed. |

### WU-2 Evidence

| Evidence | Exact result |
|---|---|
| Focused test command | `POSTGRES_HOST=127.0.0.1 POSTGRES_PORT=55434 POSTGRES_USER=finhealth POSTGRES_PASSWORD=secret POSTGRES_DB=postgres POSTGRES_TEST_HOST=127.0.0.1 POSTGRES_TEST_PORT=55434 POSTGRES_TEST_USER=finhealth POSTGRES_TEST_PASSWORD=secret python -m pytest tests/test_alembic.py --no-cov` |
| Focused test result | Exit 0 — 4 passed in 0.65s on PostgreSQL 16. |
| Static checks | `python -m ruff check alembic/env.py tests/test_alembic.py`; `python -m ruff format --check alembic/env.py tests/test_alembic.py alembic/versions/0001_postgresql_baseline.py`; `python -m compileall -q ...`; and `git diff --check` — all exit 0. |
| Runtime harness | A disposable `postgres:16-alpine` instance on `127.0.0.1:55434`; direct `python -m alembic upgrade head` against an empty database, then an equivalent run after creating `legacy_data`. |
| Runtime harness result | Empty bootstrap created 3 `banks`, 12 `categories`, and revision `0001_postgresql_baseline`; pre-seeded execution exited non-zero with `Refusing to initialize non-empty database` and no `banks` table. Disposable databases and container were removed. |
| Settlement | Native attempt generation 3, ordinal 3 settled `passed`; evidence revision `sha256:2fe59a0d2dd3dcd5ad6053c2ae3f33bc38f1c1f5a94caa80fd6f69dcf2d8132c`. |
| Rollback boundary | Revert `alembic.ini`, `alembic/env.py`, `alembic/versions/0001_postgresql_baseline.py`, and `tests/test_alembic.py`; restore `0001_initial.py`–`0008_phase2_recurring_rules_unique.py`. No WU-3 or WU-4 behavior is removed. |

## Deviation

None — the dependency was moved into WU-1 only to make its specified `postgresql+asyncpg` runtime harness executable. `aiosqlite` removal remains pending WU-3.

## Remaining Work

- [ ] WU-3 / Phase 3: PostgreSQL test isolation and SQLite dependency/CLI cleanup.
- [ ] WU-4 / Phase 4: Compose migration owner and operations documentation.
