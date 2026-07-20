# phase3-dashboard

## Purpose

Phase 1 ingests every line-item in a bank statement, and Phase 2 enriches each row with a closed-set category, a normalized merchant, and a recurring-charge rule. But the user still has to scroll transactions by hand to answer "where did my money go?". `phase3-dashboard` adds a server-rendered, zero-JS-chart-library dashboard that aggregates the existing `Transaction` rows into KPIs, category / merchant / monthly breakdowns, and a recurring-charges list, exposed through five JSON endpoints and a single `GET /dashboard` page. The capability is purely additive: **0 migrations**, ~1500 LOC across three chained PRs (service, API, UI), no schema changes, and the only cross-capability dependency is reusing the existing `RecurringRuleResponse` shape from Phase 2 PR #5. Every aggregation returns per-currency sub-rollups (CLP and USD shown side-by-side, no FX conversion because the app has no rate table), every endpoint accepts a `card_id` filter that defaults to a sentinel `"all"`, and the time series for the bar chart accepts a `range` window in months (3, 6, 12, or 0 for all-time). The dashboard page renders with HTMX partials and Tailwind bar tiles (no Chart.js, ApexCharts, or Plotly).

## ADDED Requirements

### Requirement: `DashboardService.summary` Returns KPI Aggregates for a Period

`DashboardService.summary(period, range, card_id)` MUST return aggregated KPI data for the calendar month `period` (ISO `YYYY-MM`) restricted to the lookback window `range` (one of `3`, `6`, `12`, or `0` for all-time) and to a single card when `card_id` is a UUID, or to every card when `card_id == "all"`. The response MUST include: `total_per_currency` (dict `{currency: amount}`, no FX conversion), `daily_avg_per_currency` (`{currency: total / calendar days of the period month}` — July divides by 31, Feb by 28/29, independent of transaction-date density), `transaction_count` (int), `transaction_count_per_currency` (`{currency: count}`; absent for empty currencies), `top_category_id` + `top_category_total_per_currency` (the closed-set category with the largest sum in any currency, broken down by currency), `top_merchant_id` + `top_merchant_total_per_currency` (analogous for merchant), `comparison_to_prev_period_pct_per_currency` (signed % change vs. the same period in the previous month, or `{}` if the previous month has no transactions in that currency), `period_start` + `period_end` (ISO dates), and `card_id` (echo of the input). `top_category_total_per_currency` and `top_merchant_total_per_currency` are also multi-currency dicts. (Q1, Q2, Q3, Q4, Q8)

#### Scenario: Summary for a single-currency period

- **GIVEN** 5 CLP transactions in `2026-07` across the "Groceries" and "Dining Out" categories
- **WHEN** the service calls `summary(period="2026-07", range=6, card_id="all")`
- **THEN** the response carries `total_per_currency == {"CLP": <sum>}` and `transaction_count == 5`
- **AND** `daily_avg_per_currency["CLP"] == total_per_currency["CLP"] / <calendar days of the period month>`
- **AND** `period_start == "2026-07-01"` and `period_end == "2026-07-31"`
- **AND** `card_id == "all"`

#### Scenario: Multi-currency period returns side-by-side

- **GIVEN** 3 CLP transactions and 2 USD transactions in `2026-07`
- **WHEN** the service calls `summary(period="2026-07", range=6, card_id="all")`
- **THEN** `total_per_currency` is `{"CLP": <clp_sum>, "USD": <usd_sum>}` with both keys present
- **AND** `daily_avg_per_currency` carries a per-currency entry for each currency present
- **AND** the response does NOT sum the two currencies into a single number

#### Scenario: Single-card filter excludes other cards

- **GIVEN** card A has 4 transactions in `2026-07` and card B has 3 transactions in `2026-07`
- **WHEN** the service calls `summary(period="2026-07", range=6, card_id=<card_A.uuid>)`
- **THEN** `transaction_count == 4` and the totals reflect only card A's rows

#### Scenario: `range=0` is all-time

- **GIVEN** transactions span `2025-01` through `2026-07`
- **WHEN** the service calls `summary(period="2026-07", range=0, card_id="all")`
- **THEN** the previous-period comparison is computed against `2026-06` (not against the dataset minimum), and `daily_avg_per_currency` uses the calendar days of the period month, not the full history

#### Scenario: Empty period returns zeros, not an error

- **GIVEN** no transactions in `2026-07`
- **WHEN** the service calls `summary(period="2026-07", range=6, card_id="all")`
- **THEN** the response is `200` with `total_per_currency == {}`, `transaction_count == 0`, `top_category_id is None`, `top_merchant_id is None`, and `comparison_to_prev_period_pct_per_currency == {}`

### Requirement: `DashboardService.categories` Returns All 12 Closed-Set Categories for a Period

`DashboardService.categories(period, card_id)` MUST return exactly 12 rows — one per seeded closed-set category from the `categories` table — for the given period, ordered by the largest single-currency total descending, then by `Category.sort_order` ascending as a stable tiebreaker. Each row MUST include `category_id`, `display_name` (e.g. "Groceries"), `total_per_currency` (dict), `transaction_count` (int, transactions in this category in this period, even if 0), and `pct_of_total` (float in `[0.0, 1.0]`, computed against the period's total in that currency, or `0.0` for zero-spend categories). Categories with zero spend in the period MUST still be present in the response with `total_per_currency == {}`, `transaction_count == 0`, and `pct_of_total == 0.0`. The "Uncategorized" closed-set category row follows the same rule (its `total_per_currency` is `{}` when nothing was tagged with it, and present as a non-zero dict otherwise). (Decision #1, #4)

#### Scenario: All 12 categories are returned even at zero spend

- **GIVEN** the `categories` table has been seeded with 12 rows and only "Groceries" has any transaction in `2026-07`
- **WHEN** the service calls `categories(period="2026-07", card_id="all")`
- **THEN** the response is a list of length 12
- **AND** the 11 non-Groceries rows each carry `total_per_currency == {}`, `transaction_count == 0`, `pct_of_total == 0.0`

#### Scenario: Categories are ordered by total descending

- **GIVEN** in `2026-07` Groceries totals CLP 100,000 and Dining Out totals CLP 50,000
- **WHEN** the service calls `categories(period="2026-07", card_id="all")`
- **THEN** the first row is `Groceries` (CLP 100,000, `pct_of_total` > 0.5) and the second is `Dining Out`

#### Scenario: Multi-currency category row

- **GIVEN** in `2026-07` Groceries has 2 CLP transactions summing to CLP 50,000 and 1 USD transaction of USD 25.00
- **WHEN** the service calls `categories(period="2026-07", card_id="all")`
- **THEN** the Groceries row carries `total_per_currency == {"CLP": 50000, "USD": 25.00}` and `transaction_count == 3`

#### Scenario: Single-card filter narrows the rows

- **GIVEN** card A has 3 Groceries transactions in `2026-07` and card B has 5 Groceries transactions in `2026-07`
- **WHEN** the service calls `categories(period="2026-07", card_id=<card_A.uuid>)`
- **THEN** the Groceries row reflects only card A's 3 transactions and the 11 zero-spend rows are still present

#### Scenario: Empty period returns 12 zero-spend rows

- **GIVEN** no transactions in `2026-07`
- **WHEN** the service calls `categories(period="2026-07", card_id="all")`
- **THEN** the response is a list of 12 rows, every row with `total_per_currency == {}` and `pct_of_total == 0.0`

### Requirement: `DashboardService.merchants` Returns Top-N Merchants by Total Spent

`DashboardService.merchants(period, card_id, limit)` MUST return the top N merchants by total spent in the period, ordered by the largest single-currency total descending, then by `Merchant.display_name` ascending as a stable tiebreaker. Each row MUST include `merchant_id`, `display_name`, `total_per_currency` (dict), `transaction_count` (int), and `last_seen_date` (ISO date of the most recent transaction for that merchant in the period, or `None` if the merchant has no transactions in the period — which can only happen if `limit` exceeds the number of distinct merchants). `limit` defaults to `10`; the response length is `min(limit, distinct_merchants_in_period)`. Merchants with `category_id IS NULL` or that were excluded by other filters are still ranked. (Q4)

#### Scenario: Top merchants ordered by total descending

- **GIVEN** in `2026-07` SHELL has CLP 30,000 (3 txns), MCDONALDS has CLP 25,000 (2 txns), and STARBUCKS has CLP 18,000 (4 txns)
- **WHEN** the service calls `merchants(period="2026-07", card_id="all", limit=10)`
- **THEN** the response is ordered `[SHELL, MCDONALDS, STARBUCKS]`
- **AND** SHELL's row carries `total_per_currency == {"CLP": 30000}`, `transaction_count == 3`, and `last_seen_date` set to the most recent SHELL transaction in `2026-07`

#### Scenario: `limit` caps the response length

- **GIVEN** 15 distinct merchants have transactions in `2026-07`
- **WHEN** the service calls `merchants(period="2026-07", card_id="all", limit=3)`
- **THEN** the response is a list of length 3 (the top three by total)

#### Scenario: Default `limit` is 10

- **GIVEN** 25 distinct merchants have transactions in `2026-07`
- **WHEN** the service calls `merchants(period="2026-07", card_id="all")` (no `limit` argument)
- **THEN** the response is a list of length 10

#### Scenario: No merchants in period returns empty list

- **GIVEN** no transactions exist in `2026-07`
- **WHEN** the service calls `merchants(period="2026-07", card_id="all", limit=10)`
- **THEN** the response is `[]` (HTTP 200, not 404)

#### Scenario: Multi-currency merchant row

- **GIVEN** in `2026-07` SHELL has 2 CLP transactions (CLP 20,000) and 1 USD transaction (USD 12.00)
- **WHEN** the service calls `merchants(period="2026-07", card_id="all", limit=10)`
- **THEN** the SHELL row carries `total_per_currency == {"CLP": 20000, "USD": 12.00}` and `transaction_count == 3`

### Requirement: `DashboardService.monthly` Returns a Time Series of Monthly Totals

`DashboardService.monthly(range, card_id)` MUST return a time series of monthly totals for the lookback window. `range` is months counted back from the current month (inclusive): `3` returns the last 3 months, `6` returns the last 6, `12` returns the last 12, and `0` returns all-time (every distinct month present in the dataset, no upper bound). Each row MUST include `month` (ISO `YYYY-MM`), `total_per_currency` (dict), `transaction_count` (int), and `prev_month_pct_per_currency` (signed % change vs. the prior calendar month, or `{}` for the earliest month in the response). The response is ordered by `month` ascending. Months with zero transactions MUST still appear in the response (with `total_per_currency == {}`, `transaction_count == 0`, and `prev_month_pct_per_currency == {}`) so the bar chart has a continuous x-axis. (Q2, Q8)

#### Scenario: `range=3` returns the last 3 months

- **GIVEN** today is `2026-07-15` and transactions exist in `2026-05`, `2026-06`, and `2026-07`
- **WHEN** the service calls `monthly(range=3, card_id="all")`
- **THEN** the response is 3 rows ordered `["2026-05", "2026-06", "2026-07"]`

#### Scenario: `range=0` returns all-time

- **GIVEN** transactions exist across 8 distinct months from `2026-01` through `2026-08`
- **WHEN** the service calls `monthly(range=0, card_id="all")`
- **THEN** the response is 8 rows ordered ascending
- **AND** the first row's `prev_month_pct_per_currency == {}`

#### Scenario: `prev_month_pct_per_currency` is the signed % change

- **GIVEN** in `2026-06` total CLP spend is 100,000 and in `2026-07` total CLP spend is 120,000
- **WHEN** the service calls `monthly(range=2, card_id="all")`
- **THEN** the `2026-07` row carries `prev_month_pct_per_currency == {"CLP": 20.0}` (or the equivalent rounded value with the same sign)

#### Scenario: Zero-transaction months are still in the series

- **GIVEN** transactions exist in `2026-05` and `2026-07` but NOT in `2026-06`
- **WHEN** the service calls `monthly(range=3, card_id="all")`
- **THEN** the response is 3 rows, and the `2026-06` row carries `total_per_currency == {}`, `transaction_count == 0`, `prev_month_pct_per_currency == {}`

#### Scenario: Multi-currency monthly row

- **GIVEN** in `2026-07` there are CLP and USD transactions
- **WHEN** the service calls `monthly(range=1, card_id="all")`
- **THEN** the single row carries `total_per_currency == {"CLP": <clp_sum>, "USD": <usd_sum>}` and `prev_month_pct_per_currency` is computed separately for each currency (or `{}` for the first month)

### Requirement: `DashboardService.recurring` Returns Active Recurring Rules for a Period

`DashboardService.recurring(period, card_id)` MUST return the active `RecurringRule` rows (`is_active=True`) that had at least one in-band occurrence (a `Transaction` row whose `date` is in `[period_start, period_end]` and whose amount is within `±15%` of the rule's median) on the same `credit_card_id` as the filter, reusing the `RecurringRuleResponse` shape from Phase 2 PR #5 unchanged. Each row includes `id`, `merchant_id`, `period_label`, `period_days`, `amount_min`, `amount_max`, `currency`, `is_active`, `confidence`, `last_seen_date`, and `occurrences`. `currency` is one currency per rule (a rule is per-currency, per Phase 2 design). The response is ordered by `last_seen_date` descending, then by `id` ascending as a tiebreaker. (Q4, Phase 2 PR #5 design D)

#### Scenario: Active rules with an in-band occurrence are returned

- **GIVEN** a `RecurringRule` for MCDONALDS with `is_active=True`, `currency="CLP"`, `last_seen_date=2026-07-05`, and an in-band transaction dated `2026-07-15`
- **WHEN** the service calls `recurring(period="2026-07", card_id="all")`
- **THEN** the response includes the MCDONALDS rule with all 11 `RecurringRuleResponse` fields populated

#### Scenario: Inactive rules are excluded

- **GIVEN** two `RecurringRule` rows for SHELL, one with `is_active=True` and one with `is_active=False`
- **WHEN** the service calls `recurring(period="2026-07", card_id="all")`
- **THEN** only the active SHELL rule is in the response

#### Scenario: Rules without an in-band occurrence in the period are excluded

- **GIVEN** a `RecurringRule` for SHELL with `is_active=True` but its most recent transaction is in `2026-05` (no in-band occurrence in `2026-07`)
- **WHEN** the service calls `recurring(period="2026-07", card_id="all")`
- **THEN** the SHELL rule is NOT in the response

#### Scenario: Single-card filter narrows the scope

- **GIVEN** card A has a recurring NETFLIX rule with an in-band transaction in `2026-07`; card B has a recurring SPOTIFY rule with an in-band transaction in `2026-07`
- **WHEN** the service calls `recurring(period="2026-07", card_id=<card_A.uuid>)`
- **THEN** only the NETFLIX rule is in the response (SPOTIFY belongs to a different card)

#### Scenario: Empty period returns `[]`

- **GIVEN** no recurring rules have an in-band occurrence in `2026-07`
- **WHEN** the service calls `recurring(period="2026-07", card_id="all")`
- **THEN** the response is `[]`

### Requirement: `GET /api/v1/dashboard/summary` Endpoint

`GET /api/v1/dashboard/summary?period=YYYY-MM&range=3|6|12|0&card_id=<uuid>|all` MUST return `200` with a JSON object matching the `SummaryResponse` Pydantic model (which wraps the `DashboardService.summary` payload). Query params: `period` is required (ISO `YYYY-MM`); `range` is optional, defaults to `6`, and MUST be one of `{3, 6, 12, 0}`; `card_id` is optional, defaults to `"all"`, and MUST be either a valid UUID or the literal string `"all"`. The endpoint MUST return `400` when `period` is missing or not a valid `YYYY-MM`, when `range` is outside the allowed set, or when `card_id` is not a UUID and not `"all"`. A `500` MUST be returned for any unhandled database error, with the underlying exception logged at `ERROR` level on `app.api.v1.dashboard`. (Q2, Q3, Q8)

#### Scenario: Happy path returns 200

- **GIVEN** 5 transactions in `2026-07`
- **WHEN** the client calls `GET /api/v1/dashboard/summary?period=2026-07`
- **THEN** the response is `200` with a JSON object that includes `total_per_currency`, `transaction_count`, `period_start`, `period_end`, and `card_id == "all"`

#### Scenario: Default `range` is 6

- **GIVEN** the client calls `GET /api/v1/dashboard/summary?period=2026-07` with no `range` param
- **WHEN** the endpoint validates input
- **THEN** `range` is bound to `6` and the service is called with `range=6`

#### Scenario: Default `card_id` is `"all"`

- **GIVEN** the client calls `GET /api/v1/dashboard/summary?period=2026-07` with no `card_id` param
- **WHEN** the endpoint validates input
- **THEN** `card_id` is bound to `"all"` and the service is called with `card_id="all"`

#### Scenario: Invalid `period` returns 400

- **GIVEN** the client calls `GET /api/v1/dashboard/summary?period=2026-7` (not zero-padded)
- **WHEN** the endpoint validates input
- **THEN** the response is `400` with a JSON error body identifying `period` as the offending field

#### Scenario: Invalid `range` returns 400

- **GIVEN** the client calls `GET /api/v1/dashboard/summary?period=2026-07&range=4`
- **WHEN** the endpoint validates input
- **THEN** the response is `400` with a JSON error body identifying `range` as the offending field

#### Scenario: Invalid `card_id` returns 400

- **GIVEN** the client calls `GET /api/v1/dashboard/summary?period=2026-07&card_id=not-a-uuid`
- **WHEN** the endpoint validates input
- **THEN** the response is `400` with a JSON error body identifying `card_id` as the offending field

### Requirement: `GET /api/v1/dashboard/categories` Endpoint

`GET /api/v1/dashboard/categories?period=YYYY-MM&card_id=<uuid>|all` MUST return `200` with a JSON array of exactly 12 `CategoryBreakdown` objects. Query params: `period` is required (ISO `YYYY-MM`); `card_id` is optional, defaults to `"all"`. The 12-row guarantee is enforced both at the service layer (Requirement: `DashboardService.categories`) and re-asserted in the response shape. Same `400` / `500` rules as `summary`. (Decision #1)

#### Scenario: Returns 12 rows for a non-empty period

- **GIVEN** only "Groceries" has transactions in `2026-07`
- **WHEN** the client calls `GET /api/v1/dashboard/categories?period=2026-07`
- **THEN** the response is `200` with a JSON array of length 12

#### Scenario: Returns 12 zero-spend rows for an empty period

- **GIVEN** no transactions in `2026-07`
- **WHEN** the client calls `GET /api/v1/dashboard/categories?period=2026-07`
- **THEN** the response is `200` with a JSON array of length 12, every row with `total_per_currency == {}` and `pct_of_total == 0.0`

#### Scenario: Missing `period` returns 400

- **GIVEN** the client calls `GET /api/v1/dashboard/categories` (no `period`)
- **WHEN** the endpoint validates input
- **THEN** the response is `400` identifying `period` as the offending field

#### Scenario: Default `card_id` is `"all"`

- **GIVEN** the client calls `GET /api/v1/dashboard/categories?period=2026-07` with no `card_id` param
- **WHEN** the endpoint validates input
- **THEN** `card_id` is bound to `"all"` and the response aggregates every card

### Requirement: `GET /api/v1/dashboard/merchants` Endpoint

`GET /api/v1/dashboard/merchants?period=YYYY-MM&card_id=<uuid>|all&limit=10` MUST return `200` with a JSON array of `MerchantBreakdown` objects, ordered by the largest single-currency total descending. Query params: `period` is required; `card_id` is optional (default `"all"`); `limit` is optional (default `10`, max `50`). The response length is `min(limit, distinct_merchants_in_period)`. Same `400` / `500` rules. (Q4)

#### Scenario: Returns top merchants with default `limit=10`

- **GIVEN** 25 distinct merchants in `2026-07`
- **WHEN** the client calls `GET /api/v1/dashboard/merchants?period=2026-07`
- **THEN** the response is `200` with a JSON array of length 10, ordered by total descending

#### Scenario: Custom `limit=3` caps the response

- **GIVEN** 25 distinct merchants in `2026-07`
- **WHEN** the client calls `GET /api/v1/dashboard/merchants?period=2026-07&limit=3`
- **THEN** the response is `200` with a JSON array of length 3

#### Scenario: `limit` above the cap returns 400

- **GIVEN** the client calls `GET /api/v1/dashboard/merchants?period=2026-07&limit=200`
- **WHEN** the endpoint validates input
- **THEN** the response is `400` identifying `limit` as the offending field

#### Scenario: No merchants returns 200 with `[]`

- **GIVEN** no transactions in `2026-07`
- **WHEN** the client calls `GET /api/v1/dashboard/merchants?period=2026-07`
- **THEN** the response is `200` with a JSON array of length 0 (NOT 404)

### Requirement: `GET /api/v1/dashboard/monthly` Endpoint

`GET /api/v1/dashboard/monthly?range=3|6|12|0&card_id=<uuid>|all` MUST return `200` with a JSON array of `MonthlyDataPoint` objects, ordered by `month` ascending. Query params: `range` is optional (default `6`); `card_id` is optional (default `"all"`). This endpoint does NOT require a `period` query param (it returns a window, not a single month). `400` for invalid `range`. Same `500` rule. (Q2, Q8)

#### Scenario: Default `range=6` returns 6 months

- **GIVEN** today is `2026-07-15` and at least 6 months of history exist
- **WHEN** the client calls `GET /api/v1/dashboard/monthly`
- **THEN** the response is `200` with a JSON array of length 6, ordered by `month` ascending, the first entry being `2026-02`

#### Scenario: `range=0` returns all-time

- **GIVEN** 8 distinct months of history
- **WHEN** the client calls `GET /api/v1/dashboard/monthly?range=0`
- **THEN** the response is `200` with a JSON array of length 8

#### Scenario: Invalid `range` returns 400

- **GIVEN** the client calls `GET /api/v1/dashboard/monthly?range=4`
- **WHEN** the endpoint validates input
- **THEN** the response is `400` identifying `range` as the offending field

#### Scenario: No history returns 200 with `[]`

- **GIVEN** no transactions exist
- **WHEN** the client calls `GET /api/v1/dashboard/monthly`
- **THEN** the response is `200` with a JSON array of length 0

### Requirement: `GET /api/v1/dashboard/recurring` Endpoint

`GET /api/v1/dashboard/recurring?period=YYYY-MM&card_id=<uuid>|all` MUST return `200` with a JSON array of `RecurringRuleResponse` objects (reusing the Phase 2 PR #5 model unchanged). Query params: `period` is required; `card_id` is optional (default `"all"`). The endpoint is a thin wrapper over `DashboardService.recurring`. Same `400` / `500` rules as `summary`. (Q4, Phase 2 PR #5)

#### Scenario: Returns active rules with in-band occurrence

- **GIVEN** a MCDONALDS rule with an in-band transaction in `2026-07`
- **WHEN** the client calls `GET /api/v1/dashboard/recurring?period=2026-07`
- **THEN** the response is `200` with a JSON array that includes the MCDONALDS rule (one object per active rule)

#### Scenario: Missing `period` returns 400

- **GIVEN** the client calls `GET /api/v1/dashboard/recurring` (no `period`)
- **WHEN** the endpoint validates input
- **THEN** the response is `400` identifying `period` as the offending field

#### Scenario: Default `card_id` is `"all"`

- **GIVEN** the client calls `GET /api/v1/dashboard/recurring?period=2026-07` with no `card_id` param
- **WHEN** the endpoint validates input
- **THEN** `card_id` is bound to `"all"`

#### Scenario: No active rules in the period returns 200 with `[]`

- **GIVEN** no `RecurringRule` has an in-band occurrence in `2026-07`
- **WHEN** the client calls `GET /api/v1/dashboard/recurring?period=2026-07`
- **THEN** the response is `200` with a JSON array of length 0

### Requirement: Multi-Currency Sub-Rollup (No Conversion)

All five dashboard endpoints MUST return per-currency sub-rollups. A `total_per_currency` field MUST be a dict `{currency_code: amount}` (e.g. `{"CLP": 1234567, "USD": 89.90}`). The dashboard MUST NOT sum amounts across currencies (no FX conversion; the app has no FX rate table). If only one currency is present in the period, the dict MUST have exactly one entry. If no transactions are present, the dict MUST be empty (`{}`). `pct_of_total` and `prev_month_pct_per_currency` MUST be computed per currency (each currency's numerator over its own period total, with `0.0` for the currency's own total). (Q3)

#### Scenario: Single-currency period produces a one-entry dict

- **GIVEN** all transactions in `2026-07` are CLP
- **WHEN** the client calls `GET /api/v1/dashboard/summary?period=2026-07`
- **THEN** `total_per_currency` has exactly one key: `"CLP"`, and the response carries no `USD` entry

#### Scenario: Multi-currency period produces side-by-side entries

- **GIVEN** `2026-07` has CLP and USD transactions
- **WHEN** the client calls `GET /api/v1/dashboard/summary?period=2026-07`
- **THEN** `total_per_currency` carries both `"CLP"` and `"USD"` keys
- **AND** no field sums the two currencies into a single number
- **AND** `comparison_to_prev_period_pct_per_currency` is computed separately per currency

#### Scenario: Empty period produces empty dict

- **GIVEN** no transactions in `2026-07`
- **WHEN** the client calls any of the five dashboard endpoints
- **THEN** every `total_per_currency` field in the response is `{}` (not `null`, not a zero-valued single-entry dict)

#### Scenario: `pct_of_total` is per-currency

- **GIVEN** in `2026-07` Groceries has CLP 50,000 of 100,000 total CLP (50%) and USD 25 of 50 total USD (50%)
- **WHEN** the client calls `GET /api/v1/dashboard/categories?period=2026-07`
- **THEN** the Groceries row carries `pct_of_total == 0.5` (a single float, since `pct_of_total` is a single ratio; the per-currency effect is that the CLP numerator and CLP denominator are used, and the USD numerator and USD denominator are used, and they happen to match)
- **AND** when the two percentages differ (e.g. CLP Groceries is 50% but USD Groceries is 80%), `pct_of_total` reflects the dominant currency's share, with the test covering at least one divergent case

### Requirement: Multi-Card Aggregation with "Todas" Default

All five dashboard endpoints MUST accept `card_id` as either a valid UUID (single card) or the sentinel string `"all"` (every card). The default for `card_id` is `"all"` when the query param is omitted. When `card_id="all"`, the SQL query MUST NOT `JOIN` through `Transaction.statement_id → Statement.credit_card_id` — instead, all transactions are aggregated without that filter. When `card_id` is a UUID, the `JOIN` IS applied and only transactions on the matching card are aggregated. A `400` MUST be returned for any other value, including `null`, empty string, a non-UUID string, or the literal string `"none"`. (Q1)

#### Scenario: Omitted `card_id` defaults to `"all"`

- **GIVEN** the client calls `GET /api/v1/dashboard/summary?period=2026-07` (no `card_id` param)
- **WHEN** the endpoint processes the request
- **THEN** the service is invoked with `card_id="all"`
- **AND** the response's `card_id` field echoes `"all"`

#### Scenario: `card_id=<uuid>` filters to a single card

- **GIVEN** card A has 5 transactions and card B has 3 transactions in `2026-07`
- **WHEN** the client calls `GET /api/v1/dashboard/summary?period=2026-07&card_id=<card_A.uuid>`
- **THEN** the service is invoked with that UUID
- **AND** `transaction_count == 5`

#### Scenario: `card_id="all"` omits the credit-card JOIN

- **GIVEN** the service receives `card_id="all"`
- **WHEN** it builds the SQL
- **THEN** no `JOIN` against the `statements` table is generated for the card filter
- **AND** the test asserts the rendered SQL does not contain a card-filter clause

#### Scenario: Empty string `card_id` returns 400

- **GIVEN** the client calls `GET /api/v1/dashboard/summary?period=2026-07&card_id=`
- **WHEN** the endpoint validates input
- **THEN** the response is `400` identifying `card_id` as the offending field

#### Scenario: Non-UUID non-`"all"` `card_id` returns 400

- **GIVEN** the client calls `GET /api/v1/dashboard/summary?period=2026-07&card_id=none`
- **WHEN** the endpoint validates input
- **THEN** the response is `400` identifying `card_id` as the offending field

### Requirement: Time-Range Filter for `monthly` Endpoint

`GET /api/v1/dashboard/monthly?range=N` MUST return the last `N` months of data, counted back from the current month (inclusive). `range=3` returns the last 3 months (including the current month); `range=6` returns the last 6; `range=12` returns the last 12; `range=0` returns all-time (every distinct month present in the dataset, no upper bound). The response is ordered by `month` ascending. Months with zero transactions MUST still be present in the response (Requirement: `DashboardService.monthly` covers the contract; this requirement only fixes the window-length semantics). (Q2, Q8)

#### Scenario: `range=3` from `2026-07-15` returns February, March, June, July

- **GIVEN** today is `2026-07-15` and transactions exist in `2026-02`, `2026-03`, `2026-06`, and `2026-07`
- **WHEN** the client calls `GET /api/v1/dashboard/monthly?range=3`
- **THEN** the response is `["2026-05", "2026-06", "2026-07"]` (the 3 months ending in the current month, with `2026-05` filled as a zero row even when it has no data)

#### Scenario: `range=12` from `2026-07-15` returns 12 months

- **GIVEN** today is `2026-07-15`
- **WHEN** the client calls `GET /api/v1/dashboard/monthly?range=12`
- **THEN** the response is `["2025-08", "2025-09", ..., "2026-07"]` (12 entries, the first being `2025-08`)

#### Scenario: `range=0` returns every distinct month

- **GIVEN** transactions span 8 distinct months
- **WHEN** the client calls `GET /api/v1/dashboard/monthly?range=0`
- **THEN** the response length equals the count of distinct `Transaction.date` months in the dataset, ordered ascending

#### Scenario: `range` and `card_id` combine

- **GIVEN** card A has 6 months of data and card B has 3 months of data, all within the last 6 months
- **WHEN** the client calls `GET /api/v1/dashboard/monthly?range=6&card_id=<card_B.uuid>`
- **THEN** the response is the 3 months where card B has data, ordered ascending

### Requirement: Tailwind Bar Chart Renders in `/dashboard` Page

The `GET /dashboard` page MUST render the monthly time series as Tailwind bar chart tiles — no JavaScript chart library. Each month is a tile containing a horizontal bar whose width is computed server-side as `style="width: {pct_of_max}%"` where `pct_of_max = total / max(monthly_totals) * 100`. The bar MUST render correctly with JavaScript disabled (HTMX-only or pure server-render is acceptable; the goal is no chart-library JS dependency). (Q6)

#### Scenario: Each month renders a Tailwind bar with `style="width: X%"`

- **GIVEN** the `monthly` endpoint returned rows with totals `[100, 200, 400, 0]` (max = 400)
- **WHEN** the dashboard page is rendered
- **THEN** the HTML contains four bar tiles with `style="width: 25%"`, `style="width: 50%"`, `style="width: 100%"`, and `style="width: 0%"` (or equivalent zero-width rendering)
- **AND** the test asserts the strings appear in the response body

#### Scenario: No JavaScript chart library is loaded

- **GIVEN** the dashboard page is loaded
- **WHEN** the test inspects the response body
- **THEN** the response does NOT include `<script>` tags for Chart.js, ApexCharts, Plotly, ECharts, or D3
- **AND** the test asserts no `<script src>` points at a known chart library

#### Scenario: Page renders with JavaScript disabled

- **GIVEN** the dashboard page is loaded with the test client configured to disable JavaScript
- **WHEN** the test asserts the response body
- **THEN** the bar tiles, KPI grid, category block, and recurring list are all present in the response (no JS-dependent content is missing)

### Requirement: `GET /dashboard` Page with Card Picker and Period Picker

`GET /dashboard` MUST return `200` with the full HTML page. The page MUST include: a card picker (Alpine.js dropdown, options = "Todas las cards" + every active `CreditCard` row from the database, default = "Todas"), a period picker (Alpine.js dropdown, options = current month + last 3 / 6 / 12 months + all-time, default = "current month"), and 5 sections rendering data from the 5 dashboard endpoints. Changing either picker MUST trigger an HTMX partial refresh (an `hx-get` request to the relevant endpoint, swapped into the corresponding `<div hx-target>`) — no full page reload. The `GET /dashboard` route MUST live in `app/web/router.py` and call `DashboardService` directly (not the API endpoints) for the initial render, so the first paint is a single DB roundtrip per service call. (Q1, Q2, Q6, Q7)

#### Scenario: Page returns 200 with the full layout

- **GIVEN** the user navigates to `/dashboard`
- **WHEN** the test client calls `GET /dashboard`
- **THEN** the response is `200` with `Content-Type: text/html`
- **AND** the body contains the card picker, the period picker, and the 5 sections (KPI grid, categories block, top merchants block, monthly bar chart, recurring list)

#### Scenario: Card picker defaults to "Todas las cards"

- **GIVEN** two `CreditCard` rows exist, both with `is_active=True`
- **WHEN** the dashboard page is rendered
- **THEN** the card picker `<option>` list has 3 entries: "Todas las cards" (selected by default), `<card_A.display_name>`, `<card_B.display_name>`
- **AND** "Todas las cards" has the `selected` attribute

#### Scenario: Period picker defaults to the current month

- **GIVEN** today is `2026-07-15`
- **WHEN** the dashboard page is rendered
- **THEN** the period picker `<option>` list has 5 entries: "Current month" (selected by default), "Last 3 months", "Last 6 months", "Last 12 months", "All-time"
- **AND** "Current month" has the `selected` attribute

#### Scenario: Picker change triggers an HTMX partial refresh

- **GIVEN** the user changes the card picker to a specific card
- **WHEN** the Alpine handler updates the page state
- **THEN** the relevant KPI / categories / merchants / monthly / recurring `<div>` triggers an `hx-get` request to the corresponding `/api/v1/dashboard/*` endpoint with the new `card_id` and `period`
- **AND** the response is swapped into the target `<div>` without a full page reload
- **AND** the test asserts the page HTML includes `hx-get="/api/v1/dashboard/summary?..."` (or equivalent) wiring on at least one target

#### Scenario: Inactive cards are not in the picker

- **GIVEN** two `CreditCard` rows exist: one with `is_active=True`, one with `is_active=False`
- **WHEN** the dashboard page is rendered
- **THEN** the card picker shows only the active card + "Todas las cards" (the inactive card is excluded)

## Out of Scope

A real `AnomalyDetector` service (the "anomaly flags" claim in the README is served by the top-3-by-category endpoint, which is honest about being "top spenders", not anomalies). Pre-computed `daily_spend_aggregates` table (Option B). Persisted `anomaly_flags` table (Option C). A JS chart library (Tailwind bars only per Q6). CSV / PDF export (deferred to Phase 4 — the README lists "exports" in Phase 4 per Q7). Real-time WebSocket updates (HTMX polling is enough for v1; the UI does not auto-refresh in v1). Multi-user / auth. Mobile-specific UX (the dashboard is desktop-first; mobile-friendly via Tailwind responsive utilities but no mobile-only features). Renaming or altering the existing `RecurringRuleResponse` shape from Phase 2 PR #5 (this capability reuses it as-is). Adding new closed-set categories beyond the existing 12 (the seed is frozen from Phase 2). FX conversion between CLP and USD (the app has no rate table; per-currency sub-rollups are the v1 contract). Persisting user preferences (last-selected card, last-selected period) across sessions (the defaults are always "Todas" and "current month").
