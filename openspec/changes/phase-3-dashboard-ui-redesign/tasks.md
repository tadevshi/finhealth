# Tasks: Phase 3 Dashboard UI Redesign

## Review Workload Forecast

| Field | Value |
|-------|-------|
| Estimated changed lines | ~520 (8 templates + UI test assertions) |
| 400-line budget risk | High |
| Chained PRs recommended | Yes (single PR by exception) |
| Suggested split | single PR (pre-approved `size:exception`) |
| Delivery strategy | exception-ok |
| Chain strategy | size-exception |

Decision needed before apply: No
Chained PRs recommended: No
Chain strategy: size-exception
400-line budget risk: High

### Suggested Work Units

| Unit | Goal | Likely PR | Focused test command | Runtime harness | Rollback boundary |
|------|------|-----------|----------------------|-----------------|-------------------|
| 1 | Full dashboard v6 shell + sections + UI tests | PR 1 (single, size:exception) | `pytest tests/test_web_phase3.py -q` | `uvicorn app.main:app --reload` → `GET /dashboard` at 1440px and 390px | Revert templates + `test_web_phase3.py`; router/service/API untouched |

## Phase 1: Foundation (base + shell)

- [x] 1.1 In `app/web/templates/base.html`, add overridable `dashboard_footer` block; keep default chrome.
- [x] 1.2 In `app/web/templates/dashboard.html`, override sidebar/topbar/footer; add fixed `w-60` desktop sidebar, compact mobile nav, centered `max-w-[1136px]` content.
- [x] 1.3 Link only `/dashboard`, `/transactions`, `/upload`, plus in-page `#dashboard-recurring` anchor; mark `/dashboard` active.
- [x] 1.4 Enforce 44px mobile touch targets via `min-h-11` on nav anchors.

## Phase 2: Core Presentation (sections root + hero/KPIs)

- [x] 2.1 Convert `app/web/templates/partials/dashboard_sections.html` into sole HTMX root with `id="dashboard-sections"`, hero, visible form, four KPIs, two responsive grids.
- [x] 2.2 Form uses `hx-get="/dashboard/sections"`, `hx-target="#dashboard-sections"`, `hx-swap="outerHTML"`; one selection refreshes everything.
- [x] 2.3 In `dashboard_summary.html`, render responsive `sm:grid-cols-2 xl:grid-cols-4`; CLP and USD side-by-side; structured empty state.
- [x] 2.4 In `dashboard_recurring.html`, add `id="dashboard-recurring"`; responsive rows; empty state; no anomaly/status invention.

## Phase 3: Visible Bars and Lists

- [x] 3.1 In `dashboard_categories.html`, replace hidden duplicate list with visible `h-2` percentage-width track+bar per category.
- [x] 3.2 In `dashboard_merchants.html`, render full-width rows and `size-8 flex-none grid place-items-center` initials.
- [x] 3.3 In `dashboard_monthly.html`, fit secondary grid; preserve real range/card labels and visible server-rendered bars.

## Phase 4: UI Contract Tests

- [x] 4.1 In `tests/test_web_phase3.py`, assert desktop sidebar exists, mobile top bar/nav present, links resolve to real routes, `/dashboard` active.
- [x] 4.2 Assert constrained region `max-w-[1136px]`, `lg:grid-cols-[minmax(0,680px)_minmax(0,440px)]` split, stacks at 390px without overflow.
- [x] 4.3 Assert visible form, `hx-get="/dashboard/sections"`, `hx-target="#dashboard-sections"`, `hx-swap="outerHTML"`; one request on selection change.
- [x] 4.4 Assert every category bar is visible; no hidden duplicate container; CLP/USD separate; no anomaly markup.
- [x] 4.5 Assert each of five sections renders its empty state within the swap target; inactive cards excluded from picker.

## Phase 5: Verification

- [x] 5.1 Run `pytest tests/test_web_phase3.py tests/test_dashboard.py tests/test_dashboard_selection.py tests/test_dashboard_api.py -q`; all green.
- [x] 5.2 Run `uvicorn app.main:app --reload`; manually verify `GET /dashboard` at 1440px and 390px (no overflow, valid links, touch targets ≥ 44px).
- [x] 5.3 Verification gate (full suite, dashboard-UI change scope): run `pytest -q`; confirm zero new failures attributable to the dashboard UI redesign — `tests/test_web_phase3.py`, `tests/test_dashboard.py`, `tests/test_dashboard_selection.py`, `tests/test_dashboard_api.py`, and all router/service/API tests pass; record the 24 pre-existing non-dashboard baseline failures (unrelated to this change) separately in the verification log and exclude them from the no-regression gate.

## Rollback

Revert `app/web/templates/base.html`, `app/web/templates/dashboard.html`, `app/web/templates/partials/dashboard_*.html`, and `tests/test_web_phase3.py` together. Router, service, API, schemas, and DB unchanged — no migration or data rollback needed.
