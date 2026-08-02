# Apply Progress: PostgreSQL-Only Persistence

## Work Unit

- Delivery: stacked-to-main, first autonomous slice only.
- Scope: WU-1 / Phase 1 tasks 1.1–1.9.
- Native attempt reset: explicit maintainer reset after ordinal 1 failed solely because `asyncpg` was absent.
- Native attempt: generation 1, ordinal 2, `passed`.

## Implementation

- Replaced `Settings.DATABASE_URL` with validated structured `POSTGRES_*` fields and a raw-value `URL.create("postgresql+asyncpg", ...)` property.
- Changed runtime and Alembic engine construction to consume the SQLAlchemy URL object directly.
- Removed SQLite path creation, WAL hook, dialect detection, and SQLite engine arguments at the engine boundary.
- Added focused configuration/engine-boundary tests for required values, topology validation, raw reserved password handling, URL masking, and direct URL-object engine invocation.
- Added `asyncpg>=0.30,<1.0` to runtime dependencies as the minimal WU-1 dependency correction. Removing `aiosqlite` remains exclusively in WU-3.

## Task Status

- [x] 0.0 Authority prerequisite resolved through the maintainer's explicit continuation/reset authorization.
- [x] 1.1–1.4 Configuration RED coverage.
- [x] 1.5–1.7 PostgreSQL URL-object implementation and redacted lifespan logging.
- [x] 1.8 SQLite boundary refactor.
- [x] 1.9 `asyncpg` runtime dependency correction.

## Work Unit Evidence

| Evidence | Exact result |
|---|---|
| Focused test command | `POSTGRES_USER=finhealth POSTGRES_PASSWORD=secret POSTGRES_DB=finhealth python -m pytest tests/test_config.py tests/test_db.py -k postgres_only --no-cov` |
| Focused test result | Exit 0 — 8 passed, 20 deselected in 0.02s. |
| Static checks | `python -m ruff check app/core/config.py app/core/lifespan.py app/db/engine.py app/db/session.py alembic/env.py tests/test_config.py` — exit 0; `python -m ruff format --check ...` — exit 0, 6 files already formatted; `git diff --check` — exit 0. |
| Runtime harness command | Start disposable `postgres:16-alpine` on `127.0.0.1:55432`; then run `timeout --signal=TERM --kill-after=5s 8s env POSTGRES_USER=finhealth POSTGRES_PASSWORD=secret POSTGRES_HOST=127.0.0.1 POSTGRES_PORT=55432 POSTGRES_DB=finhealth python -m uvicorn app.main:app --host 127.0.0.1 --port 18000`. |
| Runtime harness result | Exit 0 after accepting timeout exit 124 as the planned graceful shutdown. Uvicorn logged `Application startup complete`, served on `127.0.0.1:18000`, then completed shutdown cleanly. This proves the `asyncpg` dialect loads and startup reaches PostgreSQL. The disposable container was removed. |
| Rollback boundary | Revert `app/core/config.py`, `app/core/lifespan.py`, `app/db/engine.py`, `app/db/session.py`, `alembic/env.py`, `tests/test_config.py`, and the WU-1 `asyncpg` line in `pyproject.toml`; no WU-2–WU-4 behavior is removed. |

## Deviation

None — the dependency was moved into WU-1 only to make its specified `postgresql+asyncpg` runtime harness executable. `aiosqlite` removal remains pending WU-3.

## Remaining Work

- [ ] WU-2 / Phase 2: destructive PostgreSQL baseline and Alembic cleanup.
- [ ] WU-3 / Phase 3: PostgreSQL test isolation and SQLite dependency/CLI cleanup.
- [ ] WU-4 / Phase 4: Compose migration owner and operations documentation.
