## Exploration: Phase 3 Dashboard UI Redesign

### Current State
The approved redesign is being applied on `fix/dashboard-hardening`, whose uncommitted work already changes dashboard selection and data behavior. The current router builds one typed `DashboardSelection`, resolves the requested window, loads real summary/category/merchant/monthly/recurring data, and refreshes all five sections through one visible HTMX form targeting `GET /dashboard/sections`. Those hardening changes are the behavior baseline and must not be replaced by the older Alpine/per-section refresh implementation.

The visual shell regressed when `dashboard.html` dropped its dashboard-specific sidebar, top bar, page padding, constrained width, and hero. It now falls through to the generic `base.html` header/footer and renders an unconstrained `min-h-screen` content column. The dark section partials remain, but categories and merchants are compressed into a 2/3–1/3 row, there is no prominent live total, and `dashboard_categories.html` contains a hidden duplicate bar list solely to satisfy tests. Anomaly UI and its route/template have correctly been removed and must stay absent.

OpenPencil Dashboard v6 and Engram observations #269, #271, and #273 establish the target: a 240px desktop sidebar; a constrained 1136px content region within the main column; dark canvas, elevated hero, and card hierarchy; a live total hero; visible period/card/range filters; four KPIs; 680/440 category/merchant and monthly/recurring grids; and a 390px mobile layout with a compact top bar, responsive nav, stacked filters, and stacked content. Icons should use reliable vector/inline SVG assets rather than Unicode glyphs, and merchant initials need fixed square containers with explicit two-axis centering.

### Affected Areas
- `app/web/templates/base.html` — needs only the smallest shell extension point required to suppress or replace generic chrome on the dashboard without changing other pages.
- `app/web/templates/dashboard.html` — restore the dashboard-scoped desktop sidebar, mobile top bar/navigation, constrained page container, and unified HTMX refresh target.
- `app/web/templates/partials/dashboard_sections.html` — compose the live hero, visible selection form, and five real-data sections so the hero and labels refresh with the same selection as the cards.
- `app/web/templates/partials/dashboard_summary.html` — retain live KPI values and make the four-card grid responsive without reintroducing placeholders.
- `app/web/templates/partials/dashboard_categories.html` — replace the hidden test-only bar list with visible bars carrying the existing test contract.
- `app/web/templates/partials/dashboard_merchants.html` — give merchants adequate width and explicitly center initials in fixed avatar squares.
- `app/web/templates/partials/dashboard_monthly.html` — fit the v6 secondary grid while preserving server-rendered Tailwind bars and real range/card labels.
- `app/web/templates/partials/dashboard_recurring.html` — fit the v6 secondary grid while preserving live rules, totals, and empty state.
- `tests/test_web_phase3.py` — assert the restored responsive shell, visible controls/bars, unified HTMX swap, five sections, and continued anomaly absence; keep data/filter assertions intact.
- `app/web/router.py` and dashboard service/selection modules — behavior guardrails, not redesign targets; preserve the current typed selection, active-card filtering, context construction, and unified `/dashboard/sections` flow.

### Approaches
1. **Dashboard-scoped shell restoration** — Rebuild the v6 shell in dashboard templates while retaining the hardening router/context and unified HTMX form.
   - Pros: Smallest behavioral blast radius; preserves real data and selection semantics; isolates responsive chrome from unrelated pages; directly removes test-only markup.
   - Cons: Keeps some shell markup dashboard-specific instead of creating a reusable application-shell component.
   - Effort: Medium

2. **Generalize `base.html` into a reusable application shell** — Move desktop/mobile navigation, content constraints, and chrome configuration into shared base components used by every page.
   - Pros: Stronger long-term consistency and less duplicated navigation markup.
   - Cons: Broad cross-page regression risk, requires decisions for pages not covered by this change, and exceeds the approved dashboard-only scope.
   - Effort: High

3. **Restore the pre-hardening v5 dashboard wholesale and restyle it toward v6** — Recover the deleted sidebar/hero structure and adapt it afterward.
   - Pros: Fast visual starting point.
   - Cons: Reintroduces stale Alpine range logic, hidden selectors, anomaly wiring, hard-coded presentation, dead navigation destinations, and separate refresh behavior that conflicts with the hardening work.
   - Effort: High

### Recommendation
Use the dashboard-scoped restoration. Keep router, services, schemas, and selection behavior unchanged. Add only a minimal base extension point if necessary, then make `dashboard.html` own the desktop sidebar and responsive mobile navigation. Use valid destinations (`/dashboard`, `/transactions`, `/upload`); link recurring navigation to the real dashboard recurring section until a standalone recurring page exists, and do not add a live settings link without a route.

Place the live hero, visible `period`/`card_id`/`range_mode` form, and five sections inside the same replaceable dashboard-content partial. The form should continue requesting `/dashboard/sections`, so a selection change refreshes the hero, labels, KPIs, categories, merchants, monthly chart, and recurring list atomically. Render CLP and USD separately and never synthesize a cross-currency hero total. Use responsive grids that approximate the v6 680/440 split on desktop and stack on mobile. Convert category bars into visible markup, keep anomaly absent, use inline vector icons only where they clarify controls, and preserve server-rendered output when JavaScript is disabled.

The OpenPencil reference should guide hierarchy and proportions rather than be copied literally: its analysis reports several fixed-height overflows and low-contrast text combinations. Production Tailwind should use semantic responsive sizing, accessible contrast, and 44px mobile targets instead of reproducing those frame defects.

### Risks
- A hero outside the HTMX target would become stale after filter changes; hero, labels, and sections must share one swap boundary.
- Replacing the current form with the old Alpine segmented control would regress `range_mode` semantics and all-time/YTD behavior.
- Restoring historical nav items verbatim would create dead `/recurring` or `/settings` links because those web routes do not exist.
- Duplicate target IDs or a partial that includes its own outer target can break subsequent HTMX swaps.
- A prominent hero can accidentally imply CLP+USD conversion; currencies must remain side-by-side.
- Editing router/services during the redesign could overwrite the large uncommitted hardening diff and change business behavior outside this change.
- Literal use of OpenPencil fixed heights, muted colors, or small controls could reproduce its overflow, contrast, and touch-target warnings.

### Ready for Proposal
Yes. Propose a presentation-only, dashboard-scoped change that preserves the current hardening behavior, restores the v6 responsive shell and hierarchy, removes hidden test-only markup, keeps anomaly absent, and limits test changes to observable UI/HTMX contracts.
