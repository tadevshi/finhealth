# Proposal: PostgreSQL-Only Persistence

## Intent

Make PostgreSQL the sole persistence contract so development, tests, deployment, migrations, and operations exercise one database while preserving application/domain behavior.

## Scope

### In Scope
- Remove SQLite runtime fallback, dependencies, import/backup/restore commands, documentation, and dual-dialect tests.
- Replace undeployed Alembic history with one destructive baseline containing required schema, constraints, and seeds.
- Make structured `POSTGRES_*` settings authoritative; construct the credential-safe `postgresql+asyncpg` URL in application configuration instead of accepting a manually duplicated `DATABASE_URL`.
- Establish Compose-first deployment, PostgreSQL-backed tests, and one lifecycle runbook.

### Out of Scope
- Migrating or importing existing SQLite data.
- API, domain, model, ingestion, or dashboard behavior changes beyond PostgreSQL compatibility.
- Non-Compose production orchestration.

## Capabilities

### New Capabilities
- `postgresql-persistence`: PostgreSQL-only configuration, runtime, baseline migration, verification, Compose lifecycle, and operations.

### Modified Capabilities
- None. Existing application capability requirements remain unchanged.

## Approach

Delete SQLite branches/tooling; build SQLAlchemy URLs from structured settings; consolidate Alembic; use disposable PostgreSQL test isolation; align both Compose definitions and the runbook.

**Hard apply gate:** existing uncommitted work is governed by invalidated/escalated review authority (`authority_corrupted`). Apply MUST NOT begin until explicit maintainer resolution restores or supersedes that authority.

## Affected Areas

| Area | Impact | Description |
|------|--------|-------------|
| `app/core`, `app/db`, `app/cli`, `pyproject.toml` | Modified/Removed | PostgreSQL-only configuration and runtime; remove SQLite tooling/dependencies. |
| `alembic/` | Replaced | Destructive clean baseline. |
| `tests/` | Modified/Removed | Default PostgreSQL isolation; remove dual-dialect contracts. |
| `docker-compose*.yml`, `Dockerfile`, `.env.example`, `README.md` | Modified | Compose-first lifecycle and runbook. |

## Risks

| Risk | Likelihood | Mitigation |
|------|------------|------------|
| Local data/history loss | High | Require documented destructive reset; promise no import path. |
| PostgreSQL test slowness/flakiness | Med | Disposable isolated databases/schemas and deterministic cleanup. |
| Delivery exceeds 800 changed lines | High | After gate resolution, slice PRs by baseline/config, runtime/tests, then Compose/runbook; each slice independently verified and reversible. |

## Rollback Plan

Before deployment, revert slices and recreate the disposable volume. After deployment, restore a pre-change PostgreSQL backup; never restore SQLite behavior or old Alembic history.

## Dependencies

- Explicit maintainer review-authority resolution; PostgreSQL 16/Compose available for normal verification.

## Proposal Question Round

- Assumptions needing maintainer confirmation: no production data exists; destructive local reset is acceptable; Compose is the supported first slice, not every orchestrator.

## Success Criteria

- [ ] No SQLite runtime, fallback, import, operations, dependency, documentation, or dual-dialect test remains.
- [ ] Empty-volume migration, seeds/constraints, app health, restart persistence, backup/restore, and normal tests pass on PostgreSQL.
- [ ] Existing application/domain behavior remains unchanged.
