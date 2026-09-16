# Delta for phase3-dashboard

## MODIFIED Requirements

### Requirement: `DashboardService.summary` Returns KPI Aggregates for a Period

`DashboardService.summary(period, range, card_id)` MUST return aggregated KPI data for ISO `YYYY-MM` period, `range ∈ {3,6,12,0}` (0 = all-time), and `card_id ∈ UUID | "all"`. Response fields: `total_per_currency` (`{currency: amount}`, no FX), `daily_avg_per_currency` (`{currency: total / calendar_days_of_period_month}` — July divides by 31, Feb by 28/29, independent of transaction-date density), `transaction_count` (int), `transaction_count_per_currency` (`{currency: count}`; absent for empty currencies), `top_category_id` + `top_category_total_per_currency`, `top_merchant_id` + `top_merchant_total_per_currency`, `comparison_to_prev_period_pct_per_currency` (signed % vs. prior month or `{}`), `period_start`/`period_end` (ISO dates), `card_id` (echo).
(Previously: `daily_avg_per_currency` divided by distinct days with at least one transaction; response lacked `transaction_count_per_currency`.)

#### Scenario: Single-currency period

- **GIVEN** 5 CLP transactions in `2026-07` all dated `2026-07-01`
- **WHEN** `summary("2026-07", 6, "all")`
- **THEN** `total_per_currency == {"CLP": <sum>}`, `transaction_count == 5`, `transaction_count_per_currency == {"CLP": 5}`, `daily_avg == total/31`, `period_start/end == 2026-07-01/31`

#### Scenario: Multi-currency period

- **GIVEN** 3 CLP + 2 USD in `2026-07`
- **WHEN** `summary("2026-07", 6, "all")`
- **THEN** both keys in `total_per_currency` and `transaction_count_per_currency`; currencies not summed

#### Scenario: Single-card filter

- **GIVEN** card A: 4 txns; card B: 3 txns in `2026-07`
- **WHEN** `summary("2026-07", 6, <A>)`
- **THEN** `transaction_count == 4`; per-currency counts reflect A only

#### Scenario: `range=0` is all-time

- **GIVEN** data spans `2025-01`–`2026-07`
- **WHEN** `summary("2026-07", 0, "all")`
- **THEN** comparison vs. `2026-06`; `daily_avg` uses 31 calendar days, not full history

#### Scenario: Empty period

- **GIVEN** no transactions in `2026-07`
- **WHEN** `summary("2026-07", 6, "all")`
- **THEN** `total_per_currency == {}`, `transaction_count == 0`, `transaction_count_per_currency == {}`, top fields `None`

### Requirement: `GET /dashboard` Page with Card Picker and Period Picker

`GET /dashboard` MUST return `200` HTML with: card picker from active `CreditCard` rows (default "Todas"), period picker with explicit web modes — Current month (default), rolling 3/6/12, YTD, all-time — and 5 sections (KPI, categories, merchants, monthly, recurring). Dynamic labels (`month_name year`, `card_name`) MUST come from the current selection, NOT static text. A single HTMX GET form carrying `period`, `card_id`, `range_mode` MUST refresh every section in one composition; no hand-built Alpine URL mutation. Route in `app/web/router.py`; initial render calls `DashboardService` directly.
(Previously: Alpine dropdowns with hidden card `<select>` and hand-built `setRange()` URLs; period picker mislabeled `range=0` as "YTD"; labels were static.)

#### Scenario: Full layout

- **GIVEN** user navigates to `/dashboard`
- **WHEN** `GET /dashboard`
- **THEN** `200` HTML with pickers, 5 sections, dynamic month/card labels

#### Scenario: Active-only card picker

- **GIVEN** 2 active + 1 inactive `CreditCard`
- **WHEN** page rendered
- **THEN** picker shows "Todas" (default) + 2 active; inactive excluded

#### Scenario: Period picker modes

- **GIVEN** today is `2026-07-15`
- **WHEN** page rendered
- **THEN** picker offers Current month, Last 3/6/12 months, YTD, All-time

#### Scenario: Single HTMX form refreshes all sections

- **GIVEN** user submits the picker form
- **WHEN** single HTMX GET posts `period`, `card_id`, `range_mode`
- **THEN** all 5 sections refresh; no Alpine URL mutation

## ADDED Requirements

### Requirement: `DashboardSelection` Value Object and Date-Window Resolver

The dashboard MUST compose every partial from a server-side `DashboardSelection(period, card_id, range_mode)` with `range_mode ∈ {rolling(N), ytd, all_time}`. A pure resolver yields `(window_start, window_end)`: rolling = last N months ending in period; YTD = January 1 of period's year through `period_end`; all_time = earliest card-filtered transaction through `period_end`. Resolver MUST be deterministic with injected "today". API `range=0` MUST continue to resolve to `all_time`; API clients MUST NOT silently change.

#### Scenario: YTD window

- **GIVEN** `period="2026-07"`, `range_mode="ytd"`
- **WHEN** resolver runs
- **THEN** `window_start="2026-01-01"`, `window_end="2026-07-31"`

#### Scenario: all_time window

- **GIVEN** earliest transaction `2025-02-10`
- **WHEN** resolver with `range_mode="all_time"`, `period="2026-07"`
- **THEN** `window_start="2025-02-01"`, `window_end="2026-07-31"`

#### Scenario: API `range=0` compatibility

- **GIVEN** API client sends `range=0`
- **WHEN** endpoint translates
- **THEN** resolver receives `range_mode="all_time"` with identical pre-change behavior

### Requirement: Live Per-Currency Transaction Counts

The web summary partial MUST render each currency's count from `transaction_count_per_currency`, not from hard-coded literals.

#### Scenario: USD count derived

- **GIVEN** 2 USD transactions in `2026-07`
- **WHEN** partial renders
- **THEN** USD count displays `2`

### Requirement: Truthful Anomaly Empty State

While no anomaly detector is configured, the dashboard MUST NOT reserve a prominent empty anomaly panel or claim anomalies are available.

#### Scenario: No anomaly panel

- **GIVEN** no detector configured
- **WHEN** dashboard rendered
- **THEN** no anomaly placeholder or "no anomalies" banner
