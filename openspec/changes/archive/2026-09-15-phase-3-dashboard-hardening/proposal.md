# Proposal: Phase 3 Dashboard Hardening

## Intent

Make the dashboard accurate, filterable, safely demoable, and recoverable without changing SQLite architecture.

## Scope

### In Scope
- Define per-currency daily average as monthly total divided by calendar days; July uses 31.
- Use one server-side selection model (`period`, `card_id`, `range_mode`). Web modes distinguish rolling, YTD, and all-time; API `range=0` remains all-time.
- Render dynamic month/card labels and refresh every section through a functional active-card picker and one HTMX form.
- Render live per-currency transaction counts and remove/collapse unavailable anomaly UI.
- Make demo seeding deterministic, repeat-safe, seed-owned, and non-destructive, with normalized category aliases.
- Align SQLite paths and document persistence plus verified backup/restore procedures.

### Out of Scope
- PostgreSQL migration.
- Real anomaly detection.

## Capabilities

### New Capabilities
- `demo-data-seeding`: Repeat-safe, alias-normalized seed records that preserve unrelated data.
- `sqlite-operations`: Canonical paths, bind-mount persistence, and safe backup/restore.

### Modified Capabilities
- `phase3-dashboard`: Calendar-day averages, web range modes, unified refreshes, dynamic labels/counts, and truthful anomaly presentation.

## Approach

Add a selection value object and date-window resolver. Compose all partials from that state while preserving API compatibility. Extend summaries with per-currency counts. Reconcile only deterministic seed-owned rows. Standardize on `data/finhealth.db`; document SQLite backup semantics or stopped/checkpointed copying with restore verification.

**Assumption:** YTD begins January 1 of the selected year; all-time begins at the earliest card-filtered transaction.

## Affected Areas

| Area | Impact | Description |
|------|--------|-------------|
| `app/services/dashboard.py`, `app/schemas/dashboard.py`, `app/api/v1/dashboard.py` | Modified | Semantics, windows, counts |
| `app/web/router.py`, `app/web/templates/` | Modified | State, labels, picker, HTMX, anomaly UI |
| `app/cli/seed_demo.py` | Modified | Deterministic non-destructive seeding |
| `app/core/config.py`, `.env.example`, `docker-compose*.yml`, `README.md` | Modified | SQLite path, persistence, recovery |

## Risks

| Risk | Likelihood | Mitigation |
|------|------------|------------|
| API/web range semantics drift | Med | Typed modes and compatibility tests |
| Seed touches user data | Med | Require provenance; never reconcile unknown rows |
| Inconsistent WAL backup | Med | Backup API or stopped/checkpointed copy with restore verification |

## Rollback Plan

Revert dashboard, seed, configuration, and documentation changes together; restore the pre-change SQLite backup. No migration rollback applies.

## Dependencies

- DashboardService, HTMX, active-card data, SQLite tooling, and Docker bind mount.

## Success Criteria

- [ ] July averages equal each currency total divided by 31; API `range=0` still returns all-time.
- [ ] Card/range changes refresh every section without reload, with correct labels and counts.
- [ ] Two seed runs produce identical seed-owned rows and preserve pre-existing rows.
- [ ] Local and Docker use `data/finhealth.db`; documented backup/restore preserves verified row counts and health.
- [ ] No unavailable anomaly claim or prominent empty anomaly panel remains.
