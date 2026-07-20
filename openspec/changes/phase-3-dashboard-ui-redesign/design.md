# Design: Phase 3 Dashboard UI Redesign

## Technical Approach

Recompose the approved OpenPencil v6 hierarchy in Jinja/Tailwind while preserving the current FastAPI handlers, `DashboardSelection`, `DashboardService`, query parameters, and partial endpoints. `dashboard.html` owns dashboard-only chrome; shared pages retain the existing base header/footer. A single `dashboard_sections.html` root contains hero, visible filters, KPIs, categories, merchants, monthly history, and recurring charges, and replaces itself through HTMX.

## Architecture Decisions

| Decision | Alternatives | Rationale |
|---|---|---|
| Dashboard-scoped shell | Global app-shell refactor; restore v5 wholesale | Applies v6 without changing unrelated pages or reviving stale Alpine/anomaly/dead-link behavior. |
| One self-replacing HTMX root | Separate section requests; hero outside target | `#dashboard-sections` is the partial root; the form uses `hx-get="/dashboard/sections"`, `hx-target="#dashboard-sections"`, and `hx-swap="outerHTML"`. One selection therefore refreshes every label and datum atomically without duplicate IDs. |
| Existing semantic Tailwind tokens | Copy 65 OpenPencil colors; custom CSS/components | Reuse `canvas`, `surface*`, `border*`, `text-*`, `accent*`, and semantic status tokens from `base.html`. Static utility classes remain discoverable by Play CDN and avoid a parallel theme. |
| Server-rendered bars | JavaScript charts; hidden duplicate markup | Category and monthly tracks render visible percentage-width children from existing payloads. This is accessible, testable output and keeps the no-chart-library contract. |

**SOLID rationale:** the router/service retain data and selection responsibilities (SRP); templates only present supplied models. Dashboard blocks extend `base.html` without changing base-page consumers (OCP). Rendering depends on existing service/schema contracts rather than API calls or new view-model infrastructure (DIP/ISP), keeping this presentation change narrow.

## Data Flow

    GET /dashboard ─→ parse_selection ─→ _dashboard_context ─→ DashboardService
          │                                      │
          └─ dashboard.html ─→ #dashboard-sections (hero + form + five sections)
                                             │ change
                                             └─ GET /dashboard/sections ─→ outerHTML swap

The actual context is: `cards`, `summary`, `categories`, `merchants`, `monthly`, `recurring`, `merchants_by_id`, `period_label`, `period_iso`, `card_label`, `range_label`, `range_mode_options`, `selected_period`, `selected_range_mode`, `selected_card_id`, `recur_count`, `recur_monthly`, `window_start`, and `window_end` (`request` always; `app_name` on full-page render). Templates must not synthesize cross-currency totals: CLP/USD remain separate.

## File Changes

| File | Action | Description |
|---|---|---|
| `app/web/templates/base.html` | Modify | Add an overridable footer block only; preserve default chrome. |
| `app/web/templates/dashboard.html` | Modify | Override sidebar/topbar/footer; add fixed `w-60` desktop sidebar, compact mobile nav, and centered `max-w-[1136px]` content. Link only `/dashboard`, `/transactions`, `/upload`, plus `#dashboard-recurring`. |
| `app/web/templates/partials/dashboard_sections.html` | Modify | Become the sole HTMX root; compose hero, visible form, four KPIs, and two responsive content grids. |
| `app/web/templates/partials/dashboard_summary.html` | Modify | Responsive `sm:grid-cols-2 xl:grid-cols-4`; real per-currency hero/KPIs and structured empty state. |
| `app/web/templates/partials/dashboard_categories.html` | Modify | Replace hidden duplicate list with visible `h-2` tracks/bars. |
| `app/web/templates/partials/dashboard_merchants.html` | Modify | Full-width rows and `size-8 flex-none grid place-items-center` initials. |
| `app/web/templates/partials/dashboard_monthly.html` | Modify | Fit secondary grid; preserve real range/card labels and visible server bars. |
| `app/web/templates/partials/dashboard_recurring.html` | Modify | Add `id="dashboard-recurring"`; responsive rows and empty state, no anomaly/status invention. |
| `tests/test_web_phase3.py` | Modify | Assert visible shell/nav/form/root/bars, responsive classes, active-card exclusion, unified swap, empty states, and anomaly absence. |

## Interfaces / Contracts

No Python, route, schema, service, or API changes. Layout classes use `px-4 sm:px-6 lg:px-8`, `max-w-[1136px]`, `lg:grid-cols-[minmax(0,680px)_minmax(0,440px)]`, stacked defaults, `min-w-0`, and `min-h-11` mobile targets. Use existing token utilities; arbitrary values are limited to approved geometry. Inline SVG/Lucide paths replace Unicode control icons.

## Testing Strategy

| Layer | What | Approach |
|---|---|---|
| Unit | None | No logic changes. |
| Integration | Page/partial contracts and real context | Existing async ASGI tests inspect visible HTML, one HTMX request/root, bars, links, currencies, filters, empty states, and no hidden/anomaly markup. |
| Visual | Desktop and 390px behavior | Compare rendered layouts with v6; verify stacking, no horizontal overflow, contrast, and 44px targets. |

## Threat Matrix

N/A — no routing, shell, subprocess, VCS/PR automation, executable-file classification, or process-integration boundary changes.

## Migration / Rollout

No migration required. Templates and UI tests roll back together.

## Open Questions

- [ ] Delta spec says default `range_mode=6`, but current router behavior defaults to `current` and the proposal requires behavior unchanged. Reconcile the spec before tasks; this design preserves `current`.
