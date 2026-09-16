# Delta for phase3-dashboard

## MODIFIED Requirements

### Requirement: `GET /dashboard` Page with Responsive Shell, Live Hero, and Unified Refresh

`GET /dashboard` MUST return `200` with a server-rendered page using a dashboard-scoped responsive shell: a desktop sidebar (~240px) and a mobile compact top bar with responsive navigation. The page MUST contain a constrained content region (~1136px max), a live-data hero with per-currency totals, four KPI cards, a visible `period` / `card_id` / `range_mode` form, and five data-backed sections inside one replaceable HTMX target. Any selection change MUST submit the form to `GET /dashboard/sections`, atomically swapping the hero, labels, and all five sections. Defaults: card = "Todas" (`all`) plus every active `CreditCard`; period = current month; `range_mode` ∈ `{current, 3, 6, 12, 0}` with default `current` (Mes actual); a 6-month window is used only when the user explicitly selects it. Inactive cards MUST NOT appear. CLP/USD render side-by-side; no cross-currency aggregation. No anomaly section, placeholder, or route. No JS chart library. Route lives in `app/web/router.py` using `DashboardService` directly.
(Previously: Alpine pickers with per-section `hx-get` to `/api/v1/dashboard/*` and unconstrained content column.)

#### Scenario: Page returns 200 with shell, hero, and five sections

- **GIVEN** the user navigates to `/dashboard`
- **WHEN** the test client issues `GET /dashboard`
- **THEN** the response contains a desktop sidebar, mobile top bar/nav, constrained region, live hero, four KPI cards, visible selection form, and five sections

#### Scenario: Selection change atomically refreshes hero and all sections

- **GIVEN** the dashboard is rendered
- **WHEN** any selection control changes and the form submits
- **THEN** exactly one request hits `/dashboard/sections`
- **AND** hero, KPIs, categories, merchants, monthly, and recurring swap in one HTMX operation

#### Scenario: Defaults and inactive-card exclusion

- **GIVEN** two active and one inactive `CreditCard`
- **WHEN** the dashboard renders
- **THEN** the picker shows "Todas" (selected) plus the two active cards only; period defaults to current month; `range_mode` defaults to `current` (Mes actual)
- **AND** no anomaly link/section/placeholder appears

## ADDED Requirements

### Requirement: Desktop Shell Navigation

The sidebar MUST expose only valid route destinations: `/dashboard` (active here), `/transactions`, `/upload`. The active destination MUST be visually indicated. The sidebar MUST NOT link to `/recurring`, `/settings`, `/anomaly`, or any non-existent route.

#### Scenario: Sidebar links resolve to real routes

- **GIVEN** the sidebar is rendered
- **WHEN** each `href` is inspected
- **THEN** every link resolves to an existing endpoint and `/dashboard` is marked active

### Requirement: Mobile Navigation

At ≤ mobile breakpoint, the dashboard MUST render a compact top bar with responsive navigation exposing the same valid destinations. Touch targets MUST be ≥ 44px. No dead destinations.

#### Scenario: 390px viewport shows valid nav

- **GIVEN** viewport is 390px wide
- **WHEN** the dashboard renders
- **THEN** top bar and nav are visible with ≥ 44px targets and only valid route links

### Requirement: Constrained Responsive Container

The content region MUST cap at ~1136px and center. Category/merchant and monthly/recurring grids MUST approximate a 680/440 split on desktop and stack on mobile. No overflow at 390px.

#### Scenario: Layout constrains and stacks responsively

- **GIVEN** desktop viewport
- **WHEN** measured
- **THEN** content is centered within the max width; at 390px, grids stack without overflow

### Requirement: Visible Category Bars

`dashboard_categories.html` MUST render category bars as visible markup. No hidden duplicate bar list may exist solely to satisfy tests.

#### Scenario: Bars are visible without hidden duplicates

- **GIVEN** the categories partial renders for a period with spend
- **WHEN** the DOM is inspected
- **THEN** every category bar is visible and no hidden duplicate container exists

### Requirement: Empty States

Each of the five sections MUST render a structured empty state when its data is empty for the active selection. Empty states MUST NOT break the unified HTMX swap boundary.

#### Scenario: Empty states render within the swap target

- **GIVEN** no transactions in the selected period
- **WHEN** the dashboard or `/dashboard/sections` renders
- **THEN** each section shows its empty state and the HTMX target is fully replaced
