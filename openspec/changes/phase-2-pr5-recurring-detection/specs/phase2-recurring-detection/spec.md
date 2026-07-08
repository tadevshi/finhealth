# phase2-recurring-detection

## Purpose

Finhealth detects every line-item in a bank statement, but the user still has to scan rows by hand to spot subscriptions, utility bills, and other repeat charges. This capability introduces a deterministic, statistical recurring-transaction detector that runs at the end of every successful ingest and exposes two endpoints for the user to review and override detected rules. The detector groups transactions on the same `credit_card_id` from the last 90 days by `(merchant_id, currency)`, requires ≥3 occurrences within ±15% of the median amount, classifies the cadence by the median interval between consecutive postings, and upserts a `RecurringRule` keyed on `(merchant_id, amount_min, amount_max, currency, period_days)`. The detector runs synchronously inside the ingest request (decision #3), is always invoked on a statement that reaches `status=COMPLETED` regardless of full or partial chunk success, and is logged with `logger.info` on full success and `logger.warning` on partial success (decisions #7, #14). A detector failure does NOT fail the otherwise-successful ingest. The rule's `confidence` (0.0–1.0) is computed from occurrence count and amount consistency and is exposed in the API response (decision #10). The `RecurringRule.recurring_rule_id` FK is preserved on existing transactions when the user deactivates a rule so historical links remain auditable.

## ADDED Requirements

### Requirement: `RecurringDetector.detect` Runs at the End of Every Successful Ingest

`RecurringDetector.detect(statement)` MUST be invoked at the end of every successful `ingest_statement` after the transactions are committed and the statement is refreshed, and MUST be skipped on the dedup early-return path. The call MUST be wrapped in `try/except` so a detector failure does not fail the otherwise-successful ingest. The detector MUST emit `logger.info` on a full-success ingest and `logger.warning` on a partial-success ingest (`failed_chunks > 0`). (Decisions #3, #7, #14; design A, E)

#### Scenario: Detector runs on full-success ingest

- **GIVEN** a statement reaches `status=COMPLETED` with `failed_chunks=0`
- **WHEN** `ingest_statement` completes
- **THEN** `RecurringDetector.detect(statement)` is called
- **AND** a `logger.info` line is emitted on `app.services.recurring_detection` containing the statement id and the count of upserted rules

#### Scenario: Detector runs on partial-success ingest

- **GIVEN** a statement reaches `status=COMPLETED` with `failed_chunks > 0`
- **WHEN** `ingest_statement` completes
- **THEN** `RecurringDetector.detect(statement)` is called
- **AND** a `logger.warning` line is emitted (not `info`) on `app.services.recurring_detection` containing the statement id, the rule count, and the partial-success indicator (decision #14)

#### Scenario: Detector failure does not fail the ingest

- **GIVEN** a statement reaches `status=COMPLETED`
- **WHEN** `RecurringDetector.detect` raises an exception (DB error, unexpected payload)
- **THEN** `logger.exception` is emitted with the statement id
- **AND** the ingest still returns the statement (the exception is swallowed; the user sees a `COMPLETED` statement, not a `FAILED` one)

#### Scenario: Detector is NOT called on the dedup path

- **GIVEN** a re-upload of a statement whose `(credit_card_id, file_hash)` already exists
- **WHEN** `ingest_statement` hits the dedup early return
- **THEN** `RecurringDetector.detect` is NOT called (the early return is BEFORE the detector call, per design E)

### Requirement: Detection Algorithm Scans Last 90 Days and Groups by `(credit_card_id, merchant_id, currency)`

The detector MUST scan `Transaction` rows on the same `credit_card_id` as the just-ingested statement, restricted to `date >= statement.period_end - 90 days`, where `installment_number IS NULL`. Rows MUST be grouped by `(credit_card_id, merchant_id, currency)`. Rows with `merchant_id IS NULL` MUST be excluded. The grouping is per credit card so a recurring charge on one card does not affect another card's pattern. (Design E)

#### Scenario: 90-day window excludes older occurrences

- **GIVEN** transactions at days 0, 30, 60, 90, and 120 from the same `(credit_card_id, merchant_id, currency)`
- **WHEN** the detector runs
- **THEN** only the four occurrences within 90 days (days 0–90) are considered for a rule

#### Scenario: Installment rows are skipped

- **GIVEN** 5 transactions for the same merchant, 2 with `installment_number IS NOT NULL` (installment rows) and 3 non-installment
- **WHEN** the detector runs
- **THEN** the 3 non-installment rows are considered; the 2 installment rows are filtered out

#### Scenario: Same merchant in different currencies produces two rules

- **GIVEN** MCDONALDS has 3 occurrences in USD and 3 occurrences in CLP within 90 days
- **WHEN** the detector runs
- **THEN** two separate rules are created (one per `(merchant_id, currency)`)

#### Scenario: Same merchant + currency on two different cards produces two rules

- **GIVEN** MCDONALDS USD has 3 occurrences on credit card A and 3 occurrences on credit card B within 90 days
- **WHEN** the detector runs
- **THEN** two separate rules are created (one per `(credit_card_id, merchant_id, currency)`) — the patterns are per-card and do not interfere

### Requirement: Pattern Detection Requires ≥3 Occurrences and ±15% Amount Tolerance

For a group to qualify as a recurring pattern, the group MUST have ≥3 occurrences within 90 days AND the amount range MUST fit within ±15% of the group's median amount. Amounts outside the ±15% band MUST be excluded from the rule's `amount_min`/`amount_max` and from the `occurrences` count. (Design C)

#### Scenario: 2 occurrences do not qualify

- **GIVEN** a group with 2 occurrences in 90 days
- **WHEN** the detector runs
- **THEN** no rule is created (the 3-occurrence threshold is not met)

#### Scenario: 3 occurrences with 30% variance do not qualify

- **GIVEN** a group with 3 occurrences at $10.00, $10.50, $13.00 (the $13.00 is ~30% above the $10.25 median)
- **WHEN** the detector runs
- **THEN** no rule is created (the 15% tolerance is exceeded by the $13.00 outlier; the remaining 2 in-band rows are below the 3-occurrence threshold)

#### Scenario: 3 occurrences within tolerance create a rule

- **GIVEN** a group with 3 occurrences at $10.00, $10.50, $11.00 (10% variance, within ±15%)
- **WHEN** the detector runs
- **THEN** a rule is created with `amount_min=10.00`, `amount_max=11.00`, and `occurrences=3`

### Requirement: Period Classification by Median Interval

The detector MUST classify the cadence from the median interval between consecutive transaction dates in the group. The mapping MUST be: weekly if median interval ≤10d, biweekly if ≤18d, monthly if ≤45d, quarterly if ≤120d, yearly if ≤400d. `period_days` is the median interval rounded to the nearest integer. `period_label` is one of `weekly`, `biweekly`, `monthly`, `quarterly`, `yearly`. (Design B)

#### Scenario: Monthly classification (median interval 30 days)

- **GIVEN** a group with 3 occurrences 30 days apart
- **WHEN** the detector runs
- **THEN** the rule has `period_label="monthly"` and `period_days=30`

#### Scenario: Weekly classification (median interval 7 days)

- **GIVEN** a group with 3 occurrences 7 days apart
- **WHEN** the detector runs
- **THEN** the rule has `period_label="weekly"` and `period_days=7`

#### Scenario: Biweekly classification (median interval 14 days)

- **GIVEN** a group with 3 occurrences 14 days apart
- **WHEN** the detector runs
- **THEN** the rule has `period_label="biweekly"` and `period_days=14`

#### Scenario: Quarterly classification (median interval 90 days)

- **GIVEN** a group with 3 occurrences 90 days apart
- **WHEN** the detector runs
- **THEN** the rule has `period_label="quarterly"` and `period_days=90`

#### Scenario: Yearly classification (median interval 365 days)

- **GIVEN** a group with 3 occurrences 365 days apart
- **WHEN** the detector runs
- **THEN** the rule has `period_label="yearly"` and `period_days=365`

### Requirement: Confidence Reflects Pattern Strength and Amount Consistency

`confidence` MUST be computed as `min(1.0, occurrences / 5) * amount_consistency_factor` where `amount_consistency_factor = max(0.0, 1.0 - (amount_max - amount_min) / median_amount)`. The result MUST be in `[0.0, 1.0]`. The `confidence` column is visible in the `GET /api/v1/recurring` response. (Decision #10; design C)

#### Scenario: 5 occurrences with identical amounts yield confidence 1.0

- **GIVEN** a group with 5 occurrences at the same amount ($10.00)
- **WHEN** the detector runs
- **THEN** `confidence = min(1.0, 5/5) * max(0.0, 1.0 - 0.00/10.00) = 1.0 * 1.0 = 1.0`

#### Scenario: 3 occurrences with 10% variance yield ~0.54 confidence

- **GIVEN** a group with 3 occurrences at $10.00, $10.50, $11.00
- **WHEN** the detector runs
- **THEN** `confidence = min(1.0, 3/5) * max(0.0, 1.0 - 1.00/10.50) = 0.6 * ~0.905 = ~0.543`

#### Scenario: 10 occurrences with 5% variance yield ~0.95 confidence

- **GIVEN** a group with 10 occurrences at $10.00 to $10.50 (5% variance)
- **WHEN** the detector runs
- **THEN** `confidence = min(1.0, 10/5) * max(0.0, 1.0 - 0.50/10.25) = 1.0 * ~0.951 = ~0.951`

#### Scenario: An outlier is filtered out by the ±15% tolerance and does not tank confidence

- **GIVEN** a group with 3 occurrences at $10.00, $10.50, $100.00
- **WHEN** the detector runs
- **THEN** the $100.00 row is excluded by the ±15% tolerance; the in-band $0.50 range on a $10.25 median yields `confidence ≈ 0.6 * 0.951 ≈ 0.571` (positive, not zero — the outlier is filtered before confidence is computed)

### Requirement: Upsert Is Idempotent by `(merchant_id, amount_min, amount_max, currency, period_days)`

The detector MUST look up an existing rule by the composite key `(merchant_id, amount_min, amount_max, currency, period_days)`. On miss, a new `RecurringRule` row is created. On hit, the existing rule is UPDATED: `last_seen_date` is bumped, `occurrences` is incremented by the new in-band count, and `confidence` is recomputed. The upsert key does NOT filter on `is_active`, so an inactive rule is still updated on the next detector run (no duplicate rules are produced). (Design D)

#### Scenario: First ingest creates a new rule

- **GIVEN** no existing `RecurringRule` for `(merchant_id, amount_min, amount_max, currency, period_days)`
- **WHEN** the detector runs and a pattern qualifies
- **THEN** a new `RecurringRule` row is inserted with `is_active=True`, `occurrences=3`, `confidence` set per the formula

#### Scenario: Second ingest updates the same rule (no duplicate)

- **GIVEN** an existing rule for `(merchant_id, amount_min, amount_max, currency, period_days)` with `occurrences=3`
- **WHEN** the detector runs on the same pattern with one additional in-band occurrence
- **THEN** the existing rule is UPDATED (not a new row): `occurrences` becomes 4, `last_seen_date` is updated, `confidence` is recomputed
- **AND** no second `RecurringRule` row exists for this key

#### Scenario: Different amount range creates a separate rule

- **GIVEN** an existing monthly rule for MCDONALDS at $10.00–$10.50
- **WHEN** the detector runs on a NEW pattern at $11.00–$11.50 (same merchant, same period)
- **THEN** a second rule is created (the upsert key matches on `amount_min` and `amount_max`, so the new band does not match the existing one)

#### Scenario: Different period creates a separate rule

- **GIVEN** an existing monthly rule for MCDONALDS at $10.00–$10.50
- **WHEN** the detector runs on a NEW quarterly pattern (90 days apart) at the same amount
- **THEN** a second rule is created (the upsert key matches on `period_days` too, so the monthly key does not match the quarterly key)

### Requirement: `GET /api/v1/recurring` Lists Active Rules; `PATCH /api/v1/recurring/{id}` Toggles `is_active`

`GET /api/v1/recurring` MUST return all rules where `is_active=True`, ordered by `last_seen_date` descending, as a list of `RecurringRuleResponse` objects. `PATCH /api/v1/recurring/{id}` MUST accept `{"is_active": bool}` and update the rule's `is_active` field; the endpoint MUST return 200 with the updated rule, or 404 if the id does not exist. When the user deactivates a rule, the `recurring_rule_id` FK on existing transactions MUST be preserved (the API list filters by `is_active=True`; the historical FK is kept for audit). (Design D)

#### Scenario: GET excludes inactive rules

- **GIVEN** 3 rules where 2 have `is_active=True` and 1 has `is_active=False`
- **WHEN** the client calls `GET /api/v1/recurring`
- **THEN** the response is a list of 2 rules (the inactive one is excluded)

#### Scenario: GET orders by `last_seen_date` descending

- **GIVEN** 3 active rules with `last_seen_date` of 2026-07-01, 2026-07-05, 2026-07-03
- **WHEN** the client calls `GET /api/v1/recurring`
- **THEN** the response is ordered `[2026-07-05, 2026-07-03, 2026-07-01]`

#### Scenario: PATCH activates a rule

- **GIVEN** an inactive rule
- **WHEN** the client calls `PATCH /api/v1/recurring/{id}` with `{"is_active": true}`
- **THEN** the response is 200 with the updated rule (now active)
- **AND** a subsequent `GET /api/v1/recurring` includes the rule

#### Scenario: PATCH deactivates a rule and preserves FK on historical transactions

- **GIVEN** an active rule with 5 transactions whose `recurring_rule_id` matches the rule
- **WHEN** the client calls `PATCH /api/v1/recurring/{id}` with `{"is_active": false}`
- **THEN** the response is 200 with the updated rule (`is_active=False`)
- **AND** the 5 transactions still carry `recurring_rule_id` set to the rule's id (the FK is NOT cleared, per design D)
- **AND** a subsequent `GET /api/v1/recurring` excludes the rule

## Out of Scope

A `deactivated_at` timestamp column on `RecurringRule`; recurring override UI in the web layer (decision #12 — PATCH is the per-item override path); Phase 2 PR #6 (Docs + e2e); the `_metadata_completeness` cherry-pick (already in main); improving LLM installment detection (a future PR can improve the LLM; the `installment_number IS NULL` filter is the v1 path); recurring rule generation via LLM (the algorithm is deterministic + statistical); Sentry alerts for detector failures (decision #14: log only).
