# Apply Progress: Phase 3 Dashboard UI Redesign

## Mode

Standard mode. Strict TDD was not active: cached testing capabilities report strict TDD disabled, while current project tests are available through `pytest`.

## Completed Tasks

- [x] 1.1 Add `dashboard_footer` block in `base.html`.
- [x] 1.2 Add dashboard-scoped desktop sidebar, mobile top bar/nav, and constrained content shell.
- [x] 1.3 Link only `/dashboard`, `/transactions`, `/upload`, and `#dashboard-recurring`; mark `/dashboard` active.
- [x] 1.4 Enforce `min-h-11` mobile touch targets.
- [x] 2.1 Make `dashboard_sections.html` the sole `#dashboard-sections` HTMX root with hero, form, KPIs, and two responsive grids.
- [x] 2.2 Use unified `hx-get="/dashboard/sections"`, `hx-target="#dashboard-sections"`, `hx-swap="outerHTML"` refresh.
- [x] 2.3 Render summary with `sm:grid-cols-2 xl:grid-cols-4`, CLP/USD side-by-side, and structured empty state.
- [x] 2.4 Add `id="dashboard-recurring"`, responsive recurring rows, empty state, and no invented anomaly/status.
- [x] 3.1 Replace hidden duplicate category bars with visible `h-2` track/bar markup.
- [x] 3.2 Render full-width merchant rows with `size-8 flex-none grid place-items-center` initials.
- [x] 3.3 Fit monthly content in the secondary grid with real range/card labels and per-currency server bars.
- [x] 4.1 Add shell/nav/active-link UI contract assertions.
- [x] 4.2 Add responsive width/grid/no-overflow contract assertions.
- [x] 4.3 Add unified HTMX form/root assertions.
- [x] 4.4 Add visible category bar, no hidden duplicate, CLP/USD, and no anomaly assertions.
- [x] 4.5 Add empty-state and inactive-card picker assertions.
- [x] 5.1 Run focused dashboard/web/API test set successfully.
- [x] 5.2 Run uvicorn runtime render harness successfully for `/dashboard` and `/dashboard/sections`.
- [x] 5.3 Run full `pytest -q`; confirm the 24 known non-dashboard baseline failures and zero dashboard UI change-scoped failures.

## Work Unit Evidence

| Evidence | Required value |
|---|---|
| Focused test command and exact result | `pytest tests/test_web_phase3.py -q` → 21 passed in 3.23s. `pytest tests/test_web_phase3.py tests/test_dashboard.py tests/test_dashboard_selection.py tests/test_dashboard_api.py -q` → 80 passed in 7.44s. |
| Runtime harness command/scenario and exact result | `python -m uvicorn app.main:app --host 127.0.0.1 --port 8765` with HTTP requests to `/dashboard` and `/dashboard/sections?period=2026-07&card_id=all&range_mode=current` → both returned 200; response lengths 57444 and 48862; `dashboard-sections` present; full page contained `max-w-[1136px]`. |
| Rollback boundary | Revert `app/web/templates/base.html`, `app/web/templates/dashboard.html`, `app/web/templates/partials/dashboard_sections.html`, `dashboard_summary.html`, `dashboard_categories.html`, `dashboard_merchants.html`, `dashboard_monthly.html`, `dashboard_recurring.html`, `tests/test_web_phase3.py`, and this apply-progress/tasks update. Router/service/API changes were not made by this work unit. |

## Full Suite Status

`pytest -q` was run and reported 24 failures / 602 passes in 170.30s. The failures are known non-dashboard baseline failures in settings/e2e/ingestion/lifespan/LLM OpenCode Zen tests. There were zero dashboard UI change-scoped failures, so task 5.3 is complete under its scoped no-regression criterion.

## Files Changed

| File | Action | What Was Done |
|------|--------|---------------|
| `app/web/templates/base.html` | Modified | Added `dashboard_footer` block while preserving default footer content. |
| `app/web/templates/dashboard.html` | Modified | Replaced old dashboard-local Alpine/dead-route layout with dashboard-scoped shell, valid navigation, mobile nav, and constrained content. |
| `app/web/templates/partials/dashboard_sections.html` | Replaced | Made the partial the sole swap root containing live hero, visible filters, summary, and balanced responsive grids. |
| `app/web/templates/partials/dashboard_summary.html` | Modified | Added responsive KPI grid, robust payload maps, real USD counts, and structured empty state. |
| `app/web/templates/partials/dashboard_categories.html` | Modified | Removed hidden duplicate test-only bars and rendered visible category tracks/bars with structured empty state. |
| `app/web/templates/partials/dashboard_merchants.html` | Modified | Added data-backed empty state and full-width rows with centered initials. |
| `app/web/templates/partials/dashboard_monthly.html` | Replaced | Added responsive per-currency monthly bars, labels, and empty-state handling. |
| `app/web/templates/partials/dashboard_recurring.html` | Modified | Added anchor id, structured empty state, responsive rows, and removed invented status badge. |
| `tests/test_web_phase3.py` | Modified | Added UI contract coverage for shell, responsive geometry, unified HTMX, empty states, visible bars, no anomaly, no hidden duplicates, and payload-backed data. |
| `openspec/changes/phase-3-dashboard-ui-redesign/tasks.md` | Modified | Marked all completed tasks `[x]`, including 5.3 under the scoped no-regression criterion; recorded the 24 known non-dashboard baseline failures separately. |

## Deviations from Design

None for production code. The full repository suite still has 24 known non-dashboard baseline failures, recorded separately from the completed dashboard UI no-regression gate.

## Remaining Tasks

None. All 19 tasks are complete; the full-suite baseline remains repository health debt outside this change scope.
