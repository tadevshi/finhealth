## Exploration: Phase 3 Dashboard Hardening

### Current State
Phase 3 is implemented as a read-side `DashboardService`, five JSON endpoints, and a server-rendered `/dashboard` with HTMX partials. The archived specification and the canonical `openspec/specs/phase3-dashboard/spec.md` define per-currency totals, `card_id="all"`, current-month KPIs, rolling monthly windows, and `range=0` as all-time. No `openspec/config.yaml` exists, so there are no additional project-specific phase rules.

The daily-average issue is a semantic conflict, not merely broken division. The canonical spec, service, schema, and tests divide each currency total by distinct transaction dates. The current Docker database has 55 CLP and 10 USD July transactions, all dated `2026-07-01`; the denominator is therefore one and the KPI equals the monthly total. Prior Docker acceptance expected July total divided by 31. The hardening proposal must explicitly choose between “average per active spending day” and “average per calendar day.”

The v5 web layer has diverged from the archived contract. The visible title (`Junio 2026`) and card pill (`Santander … 0001`) are static. The real card `<select>` is hidden and has no state handler; `setRange()` captures the initial card and period, mutates hand-built URLs, and omits the merchants section. The UI calls `range=0` “YTD,” although the service and API correctly interpret `0` as all-time. The USD transaction count is hard-coded to `3`. The anomaly endpoint intentionally returns no data because detection was out of Phase 3 scope, but the page still reserves a prominent empty panel.

Demo seeding is unsafe to repeat. Category lookup lowercases names, but plan keys such as `dining`, `transport`, and `services` do not match canonical names `Dining Out`, `Transportation`, and `Bills`. Transactions are inserted unconditionally on every run; statements are selected by card and period rather than seed provenance, so synthetic rows can be attached to an existing statement. Deleting all transactions, as previously suggested, would destroy real data and is unacceptable.

Docker already bind-mounts `./data` to `/app/data`, including SQLite WAL files, but local defaults use `./finhealth.db` while Docker uses `./data/finhealth.db`; running the seed outside the container can therefore target a different database. The README incorrectly warns that `docker compose down -v` deletes bind-mounted host directories. There is no verified SQLite backup/restore runbook. These SQLite operational fixes belong here; adding PostgreSQL, `asyncpg`, a database service, or rewriting SQLite-specific SQL is a separate migration change.

### Affected Areas
- `openspec/changes/archive/2026-07-13-phase-3-dashboard/specs/phase3-dashboard/spec.md` — immutable baseline whose daily-average, picker, and range contracts the hardening delta must explicitly modify.
- `openspec/specs/phase3-dashboard/spec.md` — current canonical capability contract to be updated only through the later delta-spec/archive flow.
- `app/services/dashboard.py` — daily-average denominator, explicit range/window semantics, and per-currency transaction counts.
- `app/schemas/dashboard.py`, `app/api/v1/dashboard.py` — additive summary count shape and backward-compatible range validation.
- `app/web/router.py`, `app/web/templates/dashboard.html` — canonical dashboard selection state, dynamic month/card labels, and functional HTMX filtering.
- `app/web/templates/partials/dashboard_summary.html` — live USD count and clarified daily-average label.
- `app/web/templates/partials/dashboard_monthly.html`, `app/web/templates/partials/dashboard_anomalies.html` — truthful range context and removal/collapse of the unavailable anomaly panel.
- `app/cli/seed_demo.py` — case-normalized canonical mappings, deterministic seed ownership, and repeat-safe upserts that never delete user rows.
- `tests/test_dashboard.py`, `tests/test_dashboard_api.py`, `tests/test_web_phase3.py`, new seed tests — replace tests that currently enshrine active-day and `YTD == all-time` behavior with end-to-end state and idempotency coverage.
- `app/core/config.py`, `.env.example`, `docker-compose.yml`, `docker-compose.self-hosted.yml`, `README.md` — one canonical SQLite location, persistence verification, and backup/restore documentation.

### Approaches
1. **Preserve contracts and relabel the UI** — Keep distinct-transaction-day averaging, rename the KPI to “average per active spending day,” relabel `range=0` as all-time, wire the existing controls, derive the USD count, hide the empty anomaly card, and independently harden seed/persistence behavior.
   - Pros: Smallest dashboard/API change; archived service semantics remain intact; low compatibility risk.
   - Cons: Does not satisfy the observed July `/ 31` expectation or provide true YTD; retains overloaded integer range state and fragmented client URL mutation.
   - Effort: Medium

2. **Coherent contract hardening with one selection model** — Change the average to calendar-day semantics, introduce a small range/window resolver with distinct rolling, YTD, and all-time modes, parse card/period/range once, and let one HTMX filter form refresh a composed dashboard partial. Add per-currency counts, remove the unavailable anomaly allocation, make seed rows deterministic and provenance-scoped, and document the existing SQLite deployment safely.
   - Pros: Fixes root semantics instead of labels; one server-side state source keeps initial render and HTMX refreshes consistent; remains inside the current clean service/web boundaries; no schema migration or chart library.
   - Cons: Requires a Phase 3 delta spec and compatibility handling for existing `range=0`; more tests must change than in a cosmetic patch.
   - Effort: Medium

### Recommendation
Use approach 2 as the minimal clean-architecture solution.

The required denominator decision should be: **daily average = period total divided by the number of calendar days in the selected calendar month, per currency**. July therefore always uses 31, independent of transaction-date density or card. Empty currencies remain absent. This intentionally replaces the archived distinct-active-day contract because the current contract makes a one-date import indistinguishable from the monthly total; if the product instead wants active-day behavior, it must keep approach 1’s explicit label rather than calling the current value a general daily average.

Represent the selection as a small server-side value object (`period`, `card_id`, `range_mode`) with a pure date-window resolver. Keep legacy API `range=0` meaning all-time for compatibility, but give the web/UI explicit `ytd` and `all_time` modes: YTD starts January 1 of the selected/current year, while all-time starts at the earliest filtered transaction. Render the visible card control from active database cards and use a single HTMX GET form/partial composition so every section receives the same state; remove hand-built Alpine URLs. Add `transaction_count_per_currency` to the summary response and render the USD count from it.

Do not build anomaly detection in this change. Remove or collapse the panel until a real detector exists; an honest absence is better than a permanent empty warning card. For demo data, use exact canonical category aliases plus Unicode/case normalization, deterministic UUID/file-hash keys, and seed-owned statements/transactions. Re-running must update or skip only those deterministic rows and must leave pre-existing banks, cards, statements, and transactions untouched; do not use table-wide deletes.

Keep SQLite and the current bind-mount architecture for this change. Align local and Docker defaults on `data/finhealth.db`, document that relative bind mounts are checkout-dependent, correct the `down -v` claim, and add tested backup/restore commands that create a consistent SQLite backup (using SQLite’s backup API or a stopped-container copy including WAL handling), verify it, restore with the app stopped, and health-check/count rows after restart. PostgreSQL remains a separately proposed migration because it changes driver, engine configuration, Compose topology, SQL dialect behavior (`strftime`), migrations, and test infrastructure.

### Risks
- Changing the denominator deliberately breaks the archived Phase 3 expectation and requires an explicit MODIFIED requirement with replacement scenarios.
- Adding YTD while preserving `range=0` needs a compatibility boundary so API clients do not silently change from all-time to YTD.
- Seed provenance must be unambiguous before any reconciliation; rows not provably seed-owned must never be updated or deleted.
- SQLite backups can be inconsistent if the main file is copied while WAL writes are active; the runbook must use the backup API or stop/checkpoint the app.
- Date-window tests must inject/anchor “today” to avoid month-boundary and timezone flakiness.

### Ready for Proposal
Yes. The proposal should codify the calendar-day denominator decision, one canonical dashboard selection/range model, non-destructive deterministic demo seeding, and SQLite persistence/runbook work, while explicitly excluding anomaly detection and the PostgreSQL migration.
