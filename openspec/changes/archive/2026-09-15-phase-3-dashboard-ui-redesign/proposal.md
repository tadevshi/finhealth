# Proposal: Phase 3 Dashboard UI Redesign

## Intent

Apply the approved OpenPencil Dashboard v6 presentation to production templates. Restore a clear, responsive dashboard hierarchy without changing business rules, data aggregation, API contracts, or selection behavior.

## Scope

### In Scope
- Add a dashboard-scoped desktop sidebar, mobile navigation, constrained responsive shell, live-data hero, and accessible filters.
- Present KPIs, category/merchant bars and lists, monthly bars, recurring items, and structured empty states using real server data.
- Keep one visible selection form and unified `GET /dashboard/sections` HTMX refresh boundary for the hero and all five sections.
- Remove hidden test-only markup; update UI contract tests to assert visible output.
- Use only valid destinations and maintain accessible contrast and 44px mobile touch targets.

### Out of Scope
- Changes to aggregation, currency handling, card/period/range selection, APIs, services, schemas, or database state.
- An anomaly placeholder, standalone recurring/settings routes, or a global application-shell refactor.

## Capabilities

### New Capabilities
None.

### Modified Capabilities
- `phase3-dashboard`: Replace the dashboard presentation requirements with the approved responsive v6 shell, visible data visualizations, empty states, and unified HTMX refresh behavior while preserving existing data semantics.

## Approach

Keep the current typed selection and `/dashboard/sections` flow as the behavioral baseline. Add only the minimal `base.html` extension needed to suppress generic chrome, then implement dashboard-owned responsive navigation and content. Render CLP and USD separately, use server-rendered Tailwind bars, valid links (`/dashboard`, `/transactions`, `/upload`, plus an in-page recurring anchor), and no JavaScript chart library.

## Affected Areas

| Area | Impact | Description |
|------|--------|-------------|
| `app/web/templates/base.html` | Modified | Minimal dashboard chrome extension point |
| `app/web/templates/dashboard.html` | Modified | Responsive shell and navigation |
| `app/web/templates/partials/dashboard_*.html` | Modified | Hero, filters, cards, bars/lists, empty states, unified swap |
| `tests/test_web_phase3.py` | Modified | Observable responsive and HTMX contracts; anomaly remains absent |

## Risks

| Risk | Likelihood | Mitigation |
|------|------------|------------|
| Stale hero or broken swaps | Medium | One target/form; no duplicate target IDs |
| Dead navigation | Low | Link only to existing routes or section anchors |
| Visual regressions/overflow | Medium | Responsive constraints, contrast, and touch-target assertions |
| Behavioral regression | Low | Preserve router/service/API tests and selection semantics |

## Rollback Plan

Revert template and UI-test changes together; the unchanged router, service, API, and data contracts restore the prior dashboard presentation without migration or data rollback.

## Dependencies

- Approved OpenPencil Dashboard v6 reference and current dashboard hardening baseline.

## Success Criteria

- [ ] Desktop and 390px mobile layouts expose all controls and five live-data sections without overflow or dead links.
- [ ] One filter change atomically refreshes hero, labels, KPIs, categories, merchants, monthly, and recurring content.
- [ ] CLP/USD remain separate; anomaly and hidden test-only markup are absent.
- [ ] Dashboard UI and existing business/API tests pass; zero product-code behavior changes.
