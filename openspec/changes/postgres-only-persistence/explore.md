## Exploration: PostgreSQL-only persistence

### Current State
The working tree contains an uncommitted, dual-database migration: PostgreSQL 16, `asyncpg`, connection pooling, Compose services, a named `postgres_data` volume, healthchecks, PostgreSQL bootstrap coverage, portable dashboard SQL, and a SQLite-to-PostgreSQL importer were added while SQLite remained the application and test fallback.

That intermediate design conflicts with the product decision. `Settings.DATABASE_URL` still defaults to SQLite; `app/db/engine.py` branches by dialect and enables SQLite WAL; Alembic runs in batch mode for SQLite; both Compose files require a separately authored `DATABASE_URL` in addition to `POSTGRES_*`; the application image and Compose retain `/app/data`; and the README still presents SQLite development, backup/restore, and import workflows. Most database tests remain SQLite-backed, while PostgreSQL coverage is opt-in.

The existing migration review authority is a hard apply gate. A fresh `gentle-ai review validate --gate post-apply` returned `invalidated`, `allowed: false`, denial code `authority_corrupted`, and action `explicit-maintainer-action`. The current uncommitted candidate already contains 1,059 changed lines including untracked files, above the 800-line review budget.

### Affected Areas
- `app/core/config.py` — replace the SQLite `DATABASE_URL` default and duplicate URL input with authoritative PostgreSQL fields plus a safely constructed async SQLAlchemy URL.
- `app/db/engine.py`, `app/db/session.py`, `app/core/lifespan.py` — remove dialect branching and SQLite fallback while retaining pooled engine reuse, connectivity probing, credential-safe logging, and disposal.
- `alembic/env.py`, `alembic/versions/*.py` — make migration execution PostgreSQL-only and prove a clean empty-volume upgrade to head, including seed data and constraints.
- `app/cli/import_sqlite.py`, `app/cli/sqlite_ops.py` — remove importer and obsolete SQLite backup/restore operations.
- `app/services/dashboard.py` — retain PostgreSQL-correct month aggregation without a dual-dialect portability contract.
- `docker-compose.yml`, `docker-compose.self-hosted.yml`, `Dockerfile`, `.env.example` — make Compose authoritative, derive the app connection from `POSTGRES_*` without manual URL interpolation, remove the SQLite data mount, and retain the named Postgres volume and health dependency.
- `pyproject.toml` — remove `aiosqlite`, SQLite metadata, and the opt-in-only PostgreSQL test posture.
- `tests/conftest.py`, `tests/test_alembic.py`, database/service/API tests — replace SQLite fixtures with isolated PostgreSQL test databases or schemas; make PostgreSQL bootstrap and behavior part of the normal verification path.
- `tests/test_sqlite_ops.py`, `tests/test_sqlite_import.py`, `tests/test_dashboard_portability.py`, SQLite lifecycle assertions — delete obsolete contracts and replace lifecycle coverage with Postgres volume/bootstrap checks.
- `README.md`, `tests/test_documentation.py` — publish one Compose/PostgreSQL runbook covering configuration, first boot, health, persistence, backup, restore, upgrade, and destructive reset.

### Approaches
1. **Create a PostgreSQL-only Alembic baseline and PostgreSQL-backed test harness** — replace the undeployed migration history with one clean baseline, remove all SQLite runtime/operational code, and run database tests against disposable PostgreSQL isolation.
   - Pros: Matches the product decision exactly; smallest long-term conceptual surface; removes the long Alembic revision-width workaround and every SQLite migration branch; validates the actual production dialect by default.
   - Cons: Existing local databases must be discarded; migration and test rewrites are broad; delivery will likely need reviewable slices.
   - Effort: High

2. **Preserve the eight-revision Alembic lineage but convert it to PostgreSQL-only DDL** — keep revision identifiers and rewrite batch/dialect behavior, tests, configuration, operations, and documentation around PostgreSQL.
   - Pros: Retains current revision history and reduces disruption for any existing PostgreSQL development database.
   - Cons: Preserves historical complexity that production does not need; the long revision identifier still needs explicit handling; more migration code and upgrade paths must be reviewed and verified.
   - Effort: High

### Recommendation
Use approach 1 because production has no data and clean bootstrap is the only required deployment transition. Treat the baseline replacement as an explicit destructive reset for development environments. Keep model/domain behavior unchanged unless PostgreSQL verification exposes a real incompatibility.

Compose should pass `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD`, and topology fields to the app. Application configuration should construct a SQLAlchemy `postgresql+asyncpg` URL from structured components rather than concatenating or percent-encoding a password in Compose. This makes the Postgres variables authoritative, removes duplicate `DATABASE_URL` configuration, and safely handles URL-reserved characters in credentials.

The normal test path should use disposable PostgreSQL isolation, not retain SQLite as a hidden test dialect. Verification must include empty-volume `alembic upgrade head`, expected seeds/constraints, app and Postgres healthchecks, restart persistence through `postgres_data`, and the documented backup/restore commands.

Proposal/spec/design work may continue now, but apply MUST remain blocked until a maintainer resolves the corrupted/escalated review authority. Once resolved, task planning should forecast chained delivery because the current candidate is already above the 800-line budget and the PostgreSQL-only test conversion expands the blast radius.

### Risks
- Applying cleanup before explicit maintainer action would mutate paths governed by an invalidated review transaction and bypass review authority.
- Replacing Alembic history is intentionally destructive for any existing local database; the runbook must require a reset and must not imply an upgrade/import path.
- A default PostgreSQL test harness is slower and requires service lifecycle/isolation discipline; shared databases could create flaky tests.
- Raw Compose URL interpolation can corrupt credentials containing reserved characters or expose them in rendered configuration; structured URL construction is required.
- Removing SQLite reveals PostgreSQL-only type, constraint, transaction, and SQL behavior that SQLite tests previously masked.
- The change is likely to exceed the 800-line review budget unless delivered as bounded, independently verifiable slices after review authority is restored.

### Ready for Proposal
Yes. Proceed to proposal with PostgreSQL-only persistence, a clean Alembic baseline, structured Postgres configuration, PostgreSQL-backed verification, and a single Compose runbook as the target. Record a hard gate that no product-code apply may begin until explicit maintainer action restores or supersedes the invalidated review authority.
