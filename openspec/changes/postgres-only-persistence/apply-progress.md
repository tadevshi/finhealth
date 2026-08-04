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
- [x] 3.6 Removed the obsolete SQLite CLI operation boundary and its SQLite-only lifecycle coverage.
- [x] 3.7 Removed `aiosqlite`; no lockfile is tracked in this repository, and `pip check` is clean.

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

### WU-3 SQLite Operation Boundary Removal Evidence

| Evidence | Exact result |
|---|---|
| Slice | Autonomous WU-3 sub-slice: remove the obsolete SQLite CLI, SQLite-only operation/lifecycle tests, and `aiosqlite` dependency. `654` changed lines (all deletions), within the `800`-line review bound. |
| Focused test command | `python -c "import pathlib, tomllib; ..."` asserting `aiosqlite` is absent from project dependencies and `app/cli/sqlite_ops.py`, `tests/test_sqlite_ops.py`, and `tests/test_docker_lifecycle.py` are absent; followed by `git diff --check`. |
| Focused test result | Exit 0. The asserted dependency and files are absent; whitespace validation passed. |
| Dependency validation | `python -m pip check` — exit 0, `No broken requirements found.` |
| Retained integration test | `POSTGRES_USER=finhealth POSTGRES_PASSWORD=secret POSTGRES_DB=finhealth python -m pytest tests/test_alembic.py --no-cov` — exit 0, 1 passed and 3 skipped because `POSTGRES_TEST_HOST` was not configured. |
| Runtime harness | N/A: this sub-slice removes an obsolete command/test boundary and does not introduce a runtime behavior. Native `sdd-attempt acquire` returned `state: complete` for the settled WU-2 objective, so no WU-3 token was available to settle. |
| Rollback boundary | Restore `app/cli/sqlite_ops.py`, `tests/test_sqlite_ops.py`, `tests/test_docker_lifecycle.py`, and the `aiosqlite` dependency line in `pyproject.toml`; no configuration, Alembic baseline, or Compose behavior changes. |

### WU-3 PostgreSQL Fixture Attempt Evidence

| Evidence | Exact result |
|---|---|
| Native settlement | Generation 4, ordinal 4, token `sha256:7ae929d4f92b17662c3c5df78674091263daf3a183303043e3f3c21f20976fee` settled `failed`; evidence revision `sha256:2d6abed3df3b753ffcf4376bc730b4181f415191f20e73e8f1931b80c9fcbb85`. |
| Fixture foundation | Replaced the shared SQLite file fixture with an opt-in PostgreSQL administrator connection that creates a unique database before every test, passes structured `POSTGRES_*` settings, and terminates connections before dropping it. |
| Focused test command | `POSTGRES_USER=finhealth POSTGRES_PASSWORD=secret POSTGRES_DB=postgres POSTGRES_TEST_HOST=127.0.0.1 POSTGRES_TEST_PORT=55435 POSTGRES_TEST_USER=finhealth POSTGRES_TEST_PASSWORD=secret python -m pytest tests/test_config.py tests/test_db.py tests/test_models.py tests/test_lifespan.py tests/test_health.py tests/test_alembic.py --no-cov` |
| Focused test result | Exit 0 — 52 passed in 5.43s against disposable PostgreSQL 16 databases. |
| Runtime harness | A disposable `postgres:16-alpine` on `127.0.0.1:55435` backed the 52-test suite. It and all fixture-created databases were removed. |
| Blocking integration result | `tests/test_dashboard.py -k uncategorized_end_to_end_through_seed` failed because the seed marker for `recurring_rules.period_label` exceeds the PostgreSQL `varchar(16)` column; SQLite had not enforced this limit. |
| Static checks | Targeted Ruff lint passed. Targeted formatting and `git diff --check` passed after formatting the changed test files. |
| Rollback boundary | Revert `tests/conftest.py`, `tests/test_config.py`, `tests/test_db.py`, `tests/test_models.py`, `tests/test_lifespan.py`, `tests/test_dashboard.py`, `app/cli/seed_demo.py`, and the deletion of `tests/test_documentation.py`; this does not affect the WU-3 CLI/dependency removal slice. |

### WU-3 Seed Compatibility Retry Evidence

| Evidence | Exact result |
|---|---|
| Native settlement | Generation 5, ordinal 5, token `sha256:ddc04fdc4356fca22283ececf544947d0004309a40507f168987ddf0b48d3941` settled `passed`; evidence revision `sha256:cc60acbd989b3636e5f7c766046bdd93c5669a799c52aef9376317ffe76a5cbb`; corrected candidate delta: 169 changed lines within the 800-line bound. |
| Seed behavior | Recurring-rule ownership now uses the deterministic UUID plus canonical `(merchant_id, currency, period_days, period_label)` tuple. Canonical labels remain unmodified and within `varchar(16)`; a mismatched tuple raises a collision error. |
| Dashboard compatibility | All-time monthly grouping uses PostgreSQL `to_char(date, 'YYYY-MM')` rather than SQLite `strftime`. |
| Focused test command | `POSTGRES_USER=finhealth POSTGRES_PASSWORD=secret POSTGRES_DB=postgres POSTGRES_TEST_HOST=127.0.0.1 POSTGRES_TEST_PORT=55436 POSTGRES_TEST_USER=finhealth POSTGRES_TEST_PASSWORD=secret python -m pytest tests/test_config.py tests/test_db.py tests/test_models.py tests/test_lifespan.py tests/test_health.py tests/test_alembic.py tests/test_seed_demo_postgres.py tests/test_dashboard.py -k 'not TestSummary and not TestCategories and not TestMerchants and not TestMonthly and not TestRecurring and not TestCardFilter or uncategorized_with_seed_spend or uncategorized_end_to_end_through_seed' --no-cov` |
| Focused test result | Exit 0 — 61 passed, 28 deselected in 8.78s on PostgreSQL 16. |
| Runtime harness | Disposable `postgres:16-alpine` on `127.0.0.1:55436`; the container and all fixture-created databases were removed. |
| Static checks | Targeted Ruff lint, targeted format check, `git diff --check`, and `pip check` all exited 0. |
| Rollback boundary | Revert `app/cli/seed_demo.py`, `app/services/dashboard.py`, `tests/test_seed_demo_postgres.py`, and the directly affected dashboard fixture/test updates. This leaves the prior fixture and SQLite-boundary removal slices intact. |

### WU-3 Remaining Seed-Demo Fixture Evidence

| Evidence | Exact result |
|---|---|
| Native settlement | Generation 6, ordinal 6, token `sha256:ea6be63fd9e277e9f590c923fdbdb12e735936626a3b4210362bb88cd85dbeff` settled `passed`; evidence revision `sha256:3bfccd1ef85261ade07f400eb6ad0483558ccefd531bd36ad5cfc196d1b855ec`; 262 changed lines within the 800-line bound. |
| Fixture conversion | `tests/test_seed_demo.py` now uses the shared disposable PostgreSQL fixture and URL-object engine helper. Its deterministic seed, user-row preservation, collision, snapshot, and dashboard assertions remain intact. |
| Focused test | `tests/test_seed_demo.py` — exit 0, 16 passed on PostgreSQL 16. Combined seed/dashboard focus — exit 0, 21 passed and 32 deselected. |
| Runtime harness | Disposable `postgres:16-alpine` on `127.0.0.1:55437`; the container and all fixture-created databases were removed. |
| Static checks | Targeted Ruff lint, target formatting, `git diff --check`, and `pip check` all exited 0. |
| Full-suite observation | `pytest tests --no-cov` found 407 passed, 74 skipped, 17 failed, and 107 errors. The WU-3-relevant errors are stale `create_engine(test_settings)` callsites in categories, dashboard API, recurring, transactions, and web tests; LLM failures are unrelated. |
| Rollback boundary | Revert `tests/test_seed_demo.py` only; the previous shared fixture, seed compatibility, and SQLite-boundary removal slices remain independent. |

### WU-3 Remaining Engine Callsite Evidence

| Evidence | Exact result |
|---|---|
| Native settlement | Generation 7, ordinal 7, token `sha256:049956774a56ac495d9e29399a0b31e7e729b28679952a74bf3bfc4fe4ddce2b` settled `passed`; evidence revision `sha256:cc8a39731a8d7ed4a55509a270694723d7f42c3d9a1fe9d083e5b467aaad79f8`; 55 changed lines within the 800-line bound. |
| Conversion | Replaced every identified `create_engine(test_settings)` test callsite with `create_engine(test_settings.database_url)` in categories, dashboard API, e2e, ingestion, recurring, transactions, and web fixtures. |
| Focused tests | Categories/dashboard API/recurring/transactions/web suite — exit 0, 126 passed. Ingestion subset — exit 0, 44 passed, 50 skipped, 1 deselected. E2E — 5 skipped because local credentials/sample files are unavailable. |
| Runtime harness | Disposable `postgres:16-alpine` on `127.0.0.1:55438`; the container and all fixture-created databases were removed. |
| Static checks | Targeted Ruff lint, target formatting, `git diff --check`, and `pip check` all exited 0. |
| Rollback boundary | Revert only the nine converted test modules and their formatting-only edits; this preserves all earlier WU-3 slices. |

### WU-3 PostgreSQL Isolation Completion Evidence

| Evidence | Exact result |
|---|---|
| Native settlement | Generation 8, ordinal 8, token `sha256:45554afe2a2398f8367a761b30c90315476814b67169bda511484b7eecbf441a` settled `passed`; evidence revision `sha256:50bedac3676b015c4755819627847f17ce0c09a292812ccced8c54d87fcebc3e`; ledger-authoritative candidate delta: `99` changed lines, within the 800-line bound. |
| Gate-correction settlement | Generation 9, ordinal 9, token `sha256:915b3a3e2dd4132c6e7cac2aee934c4994465a33b73ea0c61c1ca3ca23ee273c` settled `passed`; evidence revision `sha256:4c05f9e439f3dcc9034a48fc36c0ce7124f152a8b3ca14053e95917fc13d8fe7`; ledger-authoritative candidate delta: `20` changed lines, within the 300-line bound. |
| Static cleanup | `! rg -n -i 'sqlite|aiosqlite|sqlite_ops|import_sqlite' app tests pyproject.toml` — exit 0; no SQLite references remain in the runtime, tests, or dependency metadata. |
| Focused test | `POSTGRES_* ... python -m pytest tests/test_config.py --no-cov` — exit 0, 15 passed. The default-topology tests now unset inherited host/port values before asserting documented defaults. |
| PostgreSQL suite | `POSTGRES_* ... python -m pytest tests --ignore=tests/test_llm_services.py --no-cov` — exit 0, 443 passed and 74 skipped on PostgreSQL 16. Skips are limited to absent `TEST_RUT` credentials and sample PDFs. |
| Complete-suite observation | Recorded reproducible `POSTGRES_* ... python -m pytest tests --no-cov` evidence — exit 1, 515 passed, 74 skipped, 16 failed. The two previously failing configuration topology tests are fixed; all 16 remaining failures are unchanged `tests/test_llm_services.py` contract failures, outside this persistence work unit and intentionally unchanged. |
| Quality checks | Targeted changed-file Ruff lint/format, `git diff --check`, and `python -m pip check` — exit 0. Configured mypy baseline: 8 errors before this correction (one WU-3 recurring-rule type-confusion regression plus seven pre-existing errors); focused mypy now reports those seven pre-existing errors, so this does not claim mypy clean. Repository-wide formatting remains non-clean only for five unrelated pre-existing LLM/PDF files. |
| Runtime harness | Disposable PostgreSQL 16 on `127.0.0.1:55439` ran the full persistence suite; per-test databases were provisioned and dropped by the fixture. Container `finhealth-wu3-full-postgres` was removed after the run. |
| Rollback boundary | Revert `tests/conftest.py`, converted PostgreSQL test modules, `app/cli/seed_demo.py`, `app/services/dashboard.py`, stale SQLite comment/metadata cleanup, and `tests/test_seed_demo_postgres.py`; WU-1/WU-2 behavior remains intact. |

## Deviation

The full suite is not globally green because 16 unchanged LLM schema/OpenCode Zen contract tests fail. The two former configuration topology failures are fixed. The maintainer explicitly allowed the unrelated LLM failures to be recorded rather than changing LLM provider behavior. Environment-gated E2E/PDF skips are documented by pytest and are not persistence failures.

## Remaining Work

- [ ] WU-4 / Phase 4: Compose migration owner and operations documentation.
