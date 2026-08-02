# Design: PostgreSQL-Only Persistence

## Technical Approach

Make PostgreSQL 16 the only database contract. Pydantic validates structured `POSTGRES_*` settings; `URL.create` receives raw values and returns the object used directly by runtime and Alembic engines. One transactional baseline replaces history, PostgreSQL fixtures replace SQLite, and one Compose migration service gates startup.

> **Hard apply gate:** design/spec/task work may continue, but apply MUST NOT begin until a maintainer explicitly restores or supersedes the invalidated/escalated review authority (`authority_corrupted`).

## Architecture Decisions

| Option | Tradeoff | Decision and rationale |
|---|---|---|
| Structured settings and `URL.create` | Explicit validation avoids ambiguous encoding and duplicated URLs | `POSTGRES_USER`, `POSTGRES_PASSWORD`, and `POSTGRES_DB` reject missing or `""`. `POSTGRES_HOST` defaults to `postgres` only when unset and rejects `""`. `POSTGRES_PORT` defaults to `5432` when unset; decimal integer strings are parsed, while empty/non-integer values and values outside `1..65535` fail before engine creation. Accepted values are not encoded, concatenated, or normalized before `URL.create("postgresql+asyncpg", ...)`. |
| URL object as engine boundary | Rendered URLs may differ from raw input | `Settings.database_url` returns a SQLAlchemy `URL`; `create_async_engine` receives that object directly in runtime and Alembic. Compose passes only the five structured fields and never assembles `DATABASE_URL`. Rendering is limited to diagnostics and may percent-escape reserved syntax; logs use `render_as_string(hide_password=True)` only. |
| Pre-DDL guarded transactional baseline | Existing databases cannot upgrade | Before `context.run_migrations`, the Alembic connection compares database heads with the sole script head. Unless already at that head, it queries `pg_catalog` for ordinary/partitioned tables in non-system schemas, excluding only `alembic_version`; any result raises a clear non-empty-database error before baseline DDL. On an empty database, baseline DDL, constraints, seeds, and version stamp execute on the same connection in one PostgreSQL transaction; any failure rolls all of them back. |
| Dedicated Compose `migrate` owner | Adds a one-shot service; prevents app replicas from racing migrations | `docker-compose.yml` alone defines `migrate`, depending on healthy `postgres`, with `command: alembic upgrade head` and `restart: "no"`. `finhealth` depends on `migrate: service_completed_successfully`. `docker-compose.self-hosted.yml` becomes an overlay and defines no migration command; Dockerfile starts only Uvicorn. Thus both Compose variants share one owner and application processes cannot migrate concurrently. |

## Data Flow

```text
raw POSTGRES_* -> Settings validation -> URL.create -> URL object -> runtime/Alembic engine
postgres healthy -> migrate preflight -> transactional baseline -> successful exit -> finhealth
```

## File Changes

| File | Action | Description |
|---|---|---|
| `app/core/config.py`, `app/core/lifespan.py`, `app/db/engine.py`, `app/db/session.py` | Modify | Structured validation, direct URL-object engine boundary, redacted logging, lifespan-owned factory. |
| `alembic/env.py`, `alembic/versions/0001_postgresql_baseline.py` | Modify/Create | Direct URL object, pre-DDL catalog preflight, one transactional baseline. |
| `alembic/versions/0001_initial.py`–`0008_phase2_recurring_rules_unique.py` | Delete | Remove undeployed lineage. |
| `app/cli/import_sqlite.py`, `app/cli/sqlite_ops.py`, obsolete SQLite tests | Delete | Remove SQLite and dual-dialect contracts. |
| `tests/`, `pyproject.toml`, lockfile | Modify | PostgreSQL isolation and dependency cleanup. |
| `docker-compose.yml`, `docker-compose.self-hosted.yml`, `Dockerfile`, `.env.example`, `README.md` | Modify | Single migration owner, structured fields, PostgreSQL lifecycle, no SQLite mount. |

## Testing Strategy

| Layer | What to Test | Approach |
|---|---|---|
| Unit | Every missing/empty/default/parse/range case; raw reserved characters; URL-object engine argument; masked logs | Pydantic and engine-double RED tests; assert no manual encoding or `DATABASE_URL` read. |
| Integration | Empty baseline; preflight against user tables; rollback after injected DDL/seed failure; constraints and isolation | PostgreSQL 16 disposable databases/schemas; inspect catalog before/after failure. |
| E2E/operations | Ordered Compose startup, migration failure blocking app, restart persistence, backup/restore/reset | Render both Compose variants; assert one migration command and `service_completed_successfully`; lifecycle smoke test. |

## Threat Matrix

| Boundary | Applicability | Design response | Planned RED tests |
|---|---|---|---|
| Documentation-like paths | N/A: no executable-file classification | Operator commands remain documentation | None |
| Git repository selection | N/A: no Git automation | No repository authority | None |
| Commit state | N/A: no commit automation | No index/worktree mutation | None |
| Push state | N/A: no push automation | No ref resolution | None |
| PR commands | N/A: no PR automation | No command composition | None |
| Compose process integration | Applicable | Only `migrate` executes Alembic; failure blocks `finhealth`; secrets are structured environment values and are not printed | RED tests for one owner across merged variants, healthy-postgres ordering, successful-completion ordering, and migration non-zero exit blocking app |

## Migration / Rollout

Confirm backup and maintainer authority, replace the old volume, run the one-shot migration, verify health, and perform a restore drill. Never target an existing user schema or offer SQLite fallback. Before deployment, rollback recreates the disposable volume; after deployment, restore the PostgreSQL backup.

## Open Questions

- [ ] Blocking: maintainer resolution of `authority_corrupted` before apply.
- [ ] Confirm no deployed PostgreSQL data/history exists before destructive rollout.
