# Proposal: Phase 3 — Dashboard (Option A)

## Intent

Finhealth extracts every line-item in a bank statement, but the user still has to scroll rows by hand to answer "where did my money go?". This change adds a server-rendered, zero-JS-chart-library dashboard that shows monthly spending trends, category breakdown, top merchants, recurring charges, and (as the v1 proxy for "anomalies") the top 3 spenders per category. **0 migrations, ~1500 LOC total, 3 chained PRs stacked-to-main**.

## Scope

### In scope (across 3 PRs)
- 5 new API endpoints under `app/api/v1/dashboard.py`:
  - `GET /api/v1/dashboard/summary?period=YYYY-MM&range=...&card_id=...` → KPI tile data
  - `GET /api/v1/dashboard/categories?period=...&card_id=...` → by-category breakdown (12 rows, includes 0-spend)
  - `GET /api/v1/dashboard/merchants?period=...&card_id=...&limit=10` → top merchants
  - `GET /api/v1/dashboard/monthly?range=...&card_id=...` → time series for the bar chart
  - `GET /api/v1/dashboard/recurring?period=...&card_id=...` → wraps the existing `RecurringDetector` output
- 1 new page `GET /dashboard` at `app/web/templates/dashboard.html` + 5 HTMX partials
- `DashboardService` in `app/services/dashboard.py` (data layer for all 5 endpoints)
- Multi-card: `card_id` filter on every endpoint; default = "Todas las cards"
- Multi-currency: side-by-side per-currency sub-rollups, NO conversion
- Time-range: current month + 3m / 6m / 12m / all-time
- Tailwind bar charts (no JS chart library)
- All 12 closed-set categories always present in `/categories` (even at $0)
- New tests in `tests/test_dashboard.py` (~30 tests)
- README "Phase 3" section

### Out of scope (deferred)
- Real `AnomalyDetector` service (top-3-by-category is the v1 proxy per Q4)
- Pre-computed `daily_spend_aggregates` table (Option B)
- Persisted `anomaly_flags` table (Option C)
- Chart library (ApexCharts, Chart.js, Plotly) — Tailwind bars only per Q6
- CSV / PDF export (Phase 4 per Q7)
- Real-time WebSocket updates (HTMX polling is enough for v1)
- Mobile-only UX (desktop-first; mobile-friendly via Tailwind responsive utilities)

## PR Breakdown (3 PRs, stacked-to-main)

### PR #8 — Data services (this worktree: `feat/phase-3-pr8-dashboard-data`)
**Files** (~600 LOC):
- `app/services/dashboard.py` (new) — `DashboardService` with 5 query methods, pure SQL `GROUP BY` on indexed columns
- `app/schemas/dashboard.py` (new) — Pydantic response models for the 5 endpoints
- `tests/test_dashboard.py` (new) — ~20 unit tests for the service layer
- `app/models/__init__.py` (+1) — re-export the new service module
- README note: "Phase 3 in progress — data layer done, endpoints and UI coming in PR #9 and #10"
- Single commit: `feat(dashboard): add DashboardService with 5 aggregation queries`

**Acceptance**: 5 service methods implemented + unit-tested, pytest ~348 → ~368, ruff + mypy clean, importable from a REPL.

### PR #9 — API endpoints (`feat/phase-3-pr9-dashboard-endpoints`, based on main after #8)
**Files** (~400 LOC):
- `app/api/v1/dashboard.py` (new) — FastAPI router with 5 endpoints + Pydantic v2 response models + DI for `DashboardService`
- `app/api/v1/router.py` (+3) — register the dashboard router
- `tests/test_dashboard_api.py` (new) — ~15 API tests (httpx `ASGITransport` pattern, mirrors `tests/test_recurring.py`)
- Single commit: `feat(api): add /api/v1/dashboard/* endpoints with 5 aggregation queries`

**Acceptance**: all 5 endpoints return 200, filters validated, pytest ~368 → ~383, ruff + mypy clean.

### PR #10 — UI (`feat/phase-3-pr10-dashboard-ui`, based on main after #9)
**Files** (~500 LOC):
- `app/web/router.py` (+30) — `GET /dashboard` route + `_query_dashboard` helper
- `app/web/templates/dashboard.html` (new) — full dashboard page with card picker, period picker, KPI grid, categories block, monthly bar chart, recurring list
- `app/web/templates/partials/dashboard_{summary,categories,merchants,monthly,recurring}.html` (new) — 5 HTMX partials
- `tests/test_web_phase3.py` (new) — ~10 web tests
- `README.md` (+50) — Phase 3 section
- Single commit: `feat(web): add /dashboard page with 5 HTMX partials and Tailwind bar charts`

**Acceptance**: `GET /dashboard` returns 200, all 5 partials return 200, pickers work, Tailwind bars render, pytest ~383 → ~393, ruff + mypy clean.

## Capabilities

### New capabilities
- `phase3-dashboard` (new) — the dashboard capability: `DashboardService` + 5 endpoints + the `/dashboard` UI

### Modified capabilities
**None** (purely additive; no existing spec'd capability changes behavior).

## Approach

### Service layer (PR #8)
Stateless `DashboardService` that takes an `AsyncSession` and exposes 5 async methods:
- `summary(period, range, card_id) -> SummaryResponse` — total, daily_avg, transaction_count, top_category, top_merchant, `comparison_to_prev_period_pct`
- `categories(period, card_id) -> list[CategoryBreakdown]` — 12 rows, ordered by total desc, **0-spend included**
- `merchants(period, card_id, limit) -> list[MerchantBreakdown]` — top N by total
- `monthly(range, card_id) -> list[MonthlyDataPoint]` — for the bar chart
- `recurring(period, card_id) -> list[RecurringRuleResponse]` — wraps the existing `RecurringDetector` output

All aggregations are pure SQL with `GROUP BY` on indexed columns: `Transaction.date`, `Transaction.category_id`, `Transaction.merchant_id`. Card filter via `Transaction.statement_id → Statement.credit_card_id`. **Multi-currency**: each method returns per-currency sub-rollups — no `SUM` across currencies.

### API layer (PR #9)
Thin FastAPI router. Each endpoint validates `period` (YYYY-MM), `range` (enum), `card_id` (UUID or "all") via Pydantic, calls the service, returns the response model. Handles 200 / 400 (invalid period) / 404 (card not found) / 500 (DB error).

### UI layer (PR #10)
Server-rendered dashboard page:
- **Header**: card picker (Alpine dropdown) + period picker (Alpine dropdown), defaults "Todas" + "6 months"
- **KPI grid** (Tailwind grid, responsive): total / daily avg / transaction count / subscriptions total — multi-currency shown as two parallel sub-grids
- **Categories block**: 12 rows with name, total, %, mini Tailwind bar (`style="width: {pct}%"`)
- **Monthly bar chart**: Tailwind bars (one per month), hover tooltips via Alpine `title` attribute
- **Recurring list**: reuses `RecurringRuleResponse` via HTMX partial
- No JS chart library

## Risks

| Risk | Likelihood | Mitigation |
|------|------------|------------|
| Per-request SQL aggregations slow at N > 10k txns | Med | Add "long-range" warning when `range=all-time` + N > 5k; pre-aggregates deferred to Option B |
| HTMX polling creates DB pressure | Low | Polling interval = 30s default; no auto-refresh in v1 |
| "Top 3 by category" feels like a fake anomaly flag | Med | README + UI explicitly label it "top spenders", not "anomalies" |
| Multi-currency side-by-side in UI is awkward | Low | Two parallel sub-grids; tests cover both |
| Card picker default ("Todas") confuses users expecting per-card | Low | UI hint + tests cover both defaults |

## Rollback Plan

Revert the merge. Each PR is a single commit so a `git revert` cleanly drops it. **0 migrations** = no Alembic down-grade needed. `DashboardService` is additive; no other code imports it. `RecurringRuleResponse` is reused as-is from Phase 2.

## Dependencies

- Phase 2 PR #5 merged (`RecurringRule` + `RecurringDetector` + `GET /api/v1/recurring`) — base is `origin/main @ 2ccc153`
- All prior PRs merged (Categories PR #2/#3, Merchants PR #4, Recurring PR #5) — confirmed via base
- The `Transaction.date`, `Transaction.category_id`, `Transaction.merchant_id` indexes from Phase 1 are sufficient

## Success Criteria

### PR #8
- [ ] `DashboardService` class with 5 async methods
- [ ] All 5 methods unit-tested (20+ tests)
- [ ] All methods return the documented Pydantic response shape
- [ ] Multi-currency sub-rollups work (CLP and USD shown separately)
- [ ] No regressions in existing tests (348 → ~368)

### PR #9
- [ ] `GET /api/v1/dashboard/{summary,categories,merchants,monthly,recurring}` all return 200
- [ ] `card_id`, `period`, `range` query params validated
- [ ] Pydantic response models in `app/schemas/dashboard.py`
- [ ] Router registered in `app/api/v1/router.py`
- [ ] 15+ API tests covering happy path + edge cases
- [ ] No regressions (368 → ~383)

### PR #10
- [ ] `GET /dashboard` returns 200 with the full HTML page
- [ ] All 5 HTMX partials return 200
- [ ] Card picker + period picker functional
- [ ] Tailwind bar chart renders for monthly data
- [ ] Multi-currency side-by-side in KPI grid
- [ ] README updated with Phase 3 section
- [ ] 10+ web tests (383 → ~393)
- [ ] No regressions; ruff + mypy clean across all 3 PRs
