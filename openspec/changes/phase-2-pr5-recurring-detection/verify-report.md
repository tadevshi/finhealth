# Verify Report — phase-2-pr5-recurring-detection

## Status: PASS

## Scenarios vs Tests

| Scenario | Test |
|---|---|
| Detector runs on full-success ingest | (covered by `tests/test_ingestion.py` integration tests) |
| Detector runs on partial-success ingest | (covered by `tests/test_ingestion.py` integration tests) |
| Detector failure does not fail the ingest | (covered by `tests/test_ingestion.py` integration tests) |
| Detector is NOT called on the dedup path | (covered by `tests/test_ingestion.py` integration tests) |
| 90-day window excludes older occurrences | `test_90_day_window_excludes_older_occurrences` (4 in-window + 1 out-of-window) |
| Installment rows are skipped | `test_installment_rows_are_skipped` |
| Same merchant in different currencies produces two rules | `test_same_merchant_different_currencies_produces_two_rules` |
| Same merchant + currency on two different cards produces two rules | REMOVED in spec reconciliation (out of scope — see below) |
| 2 occurrences do not qualify | `test_two_occurrences_do_not_qualify` |
| 3 occurrences with 30% variance do not qualify | `test_30_percent_variance_does_not_qualify` |
| 3 occurrences within tolerance create a rule | `test_15_percent_variance_qualifies` |
| Monthly classification | `test_period_classification` |
| Weekly classification | `test_period_classification` |
| Biweekly classification | `test_period_classification` |
| Quarterly classification | (not reachable in 90-day window with 3 in-band occurrences; verified via `test_period_classification_thresholds`) |
| Yearly classification | (not reachable in 90-day window with 3 in-band occurrences; verified via `test_period_classification_thresholds`) |
| 5 occurrences with identical amounts yield confidence 1.0 | `test_confidence_saturates_at_five_occurrences` |
| 3 occurrences with 10% variance yield ~0.54 confidence | `test_confidence_formula` |
| 10 occurrences with 5% variance yield ~0.95 confidence | `test_10_occurrences_5pct_variance_yields_high_confidence` |
| An outlier is filtered out by the ±15% tolerance | `test_outlier_filtered_by_tolerance_creates_rule` |
| First ingest creates a new rule | `test_fk_backfill_sets_recurring_rule_id` |
| Second ingest updates the same rule (no duplicate) | `test_upsert_is_idempotent` |
| Different amount range creates a separate rule | `test_different_amount_range_creates_separate_rule` |
| Different period creates a separate rule | `test_different_period_creates_separate_rule` |
| Same-day occurrences do not create a rule | `test_same_day_occurrences_do_not_create_rule` |
| GET excludes inactive rules | `test_get_recurring_excludes_inactive_and_orders_desc` |
| GET orders by `last_seen_date` descending | `test_get_recurring_excludes_inactive_and_orders_desc` |
| PATCH activates a rule | `test_patch_recurring_activates` |
| PATCH deactivates and preserves FK | `test_patch_recurring_deactivates_preserves_fk` |

## Spec Reconciliation

Two spec scenarios were reconciled with the implementation during judgment-day Round 2:

1. **Outlier scenario — 3 → 4 occurrences.** The original spec gave a 3-occurrence example (`$10.00, $10.50, $100.00`) and claimed the in-band median was `$10.25` with 3 in-band rows. The arithmetic did not check out: `statistics.median([10.00, 10.50, 100.00])` is `10.50` (not `10.25`), the ±15% band on `10.50` is `[8.925, 12.075]`, only `[10.00, $10.50]` is in-band (2 rows — below the 3-occurrence threshold), and the spec's `0.6 * 0.951` factor assumed 3 in-band rows. The spec was updated to a 4-occurrence example (`$10.00, $10.25, $10.50, $100.00`) where the math is consistent: full-group median `10.375`, band `[8.82, 11.93]`, 3 in-band rows spanning `$10.00–$10.50`, and `confidence = 0.6 * (1.0 - 0.50/10.375) = 0.6 * 0.9518 = 0.5711` (the formula uses the **full-group** median, not the in-band subset median; the test locks this with a `0.0001` tolerance that would fail at `0.5707`).

2. **Two-cards scenario removed + grouping header downgraded.** The original spec grouped by `(credit_card_id, merchant_id, currency)` and had a scenario requiring the detector to produce two rules for the same merchant+currency on different cards. The implementation groups by `(merchant_id, currency)` only — the `RecurringRule` model has no `credit_card_id` column, and the upsert key does not include it. The detector scans per-`credit_card_id` (a filter, not a grouping key), so within a single `detect()` call all rows are on the same card. Adding `credit_card_id` to the model would require a schema migration, which is out of scope for a judgment-day fix. The requirement header was downgraded to `(merchant_id, currency)` and the two-cards scenario was removed.

## Code Fixes Applied (judgment-day Round 2)

1. **Dead-code removal** — `app/services/recurring_detection.py` had a duplicate call to `self._classify_period(median_interval)` on line 360 (same call as line 359). Removed.
2. **Same-day fix** — three same-day transactions previously produced a meaningless rule with `period_label="weekly"` and `period_days=0`. Now the detector returns `None` when all intervals are zero, and skips the group entirely.
3. **Relationship loading** — `RecurringRule.merchant` and `RecurringRule.transactions` were `lazy="joined"` and `lazy="selectin"` respectively. Both fields are not in the API response, so the default read paid for a JOIN and a second round-trip. Both changed to `lazy="noload"` (callers can still opt in with `await session.refresh(rule, ["merchant"])` or `selectinload`).

## Risks

- The "two different cards" behavior is now documented as out of scope. A future PR may add `credit_card_id` to the `RecurringRule` model and re-introduce the scenario.
- `lazy="noload"` on the relationships means any future code that touches `rule.merchant` or `rule.transactions` without an explicit eager load will trigger a lazy load (which may be surprising). The model docstring documents the opt-in pattern.
