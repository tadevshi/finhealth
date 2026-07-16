```yaml
schema: gentle-ai.verify-result/v1
evidence_revision: sha256:97f050d6ce156b81803371657734ce12bf0255253578e51135fadd3ba5b1673b
verdict: pass
blockers: 0
critical_findings: 0
requirements: 7/7
scenarios: 27/27
test_command: python -m pytest tests/test_recurring.py tests/test_alembic.py tests/test_ingestion.py::TestRecurringDetectorIntegration -q
test_exit_code: 0
test_output_hash: sha256:b420d86c73e6674636414949a9d32adb129912ea1ed5933143cf358c98564a9b
build_command: python -m compileall -q app
build_exit_code: 0
build_output_hash: sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855
```

## Verification Report

**Change**: `phase-2-pr5-recurring-detection`
**Version**: N/A
**Mode**: Standard (`strict_tdd: false`)
**Verification date**: 2026-07-16
**Verdict**: **PASS WITH WARNINGS**

### Executive Summary

Current source inspection and runtime execution confirm all 7 requirements and all 27 scenarios. The focused recurring suite passes 33 tests, and the combined recurring, migration, and ingestion integration command passes 56 tests.

The bounded review authority is approved for lineage `review-66599bd07a9b3cc8`, and native `post-apply` validation returned `allow` because the authoritative transaction, current repository target, and content-bound artifacts match. No review hash or metadata was created or modified during verification.

The full repository suite remains non-green with the same 23 unrelated baseline failures: 546 tests pass and repository coverage is 82.81%. Repository-wide mypy and Ruff checks also retain unrelated failures outside the bounded correction. Native SDD status separately reports an archive-routing ambiguity because two approved terminal authorities exist; this does not invalidate the observed `post-apply` allow result or the change-scoped runtime evidence, but it must be resolved before archive.

### Completeness

| Metric | Value |
|---|---:|
| Requirements total | 7 |
| Requirements fully compliant | 7 |
| Scenarios total | 27 |
| Scenarios compliant | 27 |
| Tasks total | 7 |
| Tasks complete | 7 |
| Tasks incomplete | 0 |

Proposal, delta spec, design, tasks, apply progress, prior verify report, current implementation, and current tests were read directly. Native status confirms all seven tasks complete and `applyState: all_done`.

### Native Review Evidence

| Evidence | Observed result |
|---|---|
| `gentle-ai review status --cwd /home/tadashi/develop/finhealth` | Authoritative compact-v2 authority; lineage `review-66599bd07a9b3cc8` is `approved`, revision `sha256:c72c3ed706492e76bd31e670c3dafb6bc5bd3084f26fecb84d1696112e4fa7a7`, with no reported problems. |
| `gentle-ai review validate --gate post-apply --cwd /home/tadashi/develop/finhealth` | `result: allow`, `allowed: true`; reason: authoritative transaction, current repository target, and content-bound artifacts match. |
| `gentle-ai sdd-status phase-2-pr5-recurring-detection --cwd /home/tadashi/develop/finhealth --json --instructions` | Verification artifacts/tasks are all done, but archive routes to `resolve-review` because multiple terminal native review receipts are present and the change-local mirror is absent. |

The approved receipt/authority and post-apply allow result are cited exactly as observed. No hashes were inferred or fabricated.

### Test and Tooling Evidence

| Check | Exit | Result | Exact output SHA-256 |
|---|---:|---|---|
| `python -m pytest tests/test_recurring.py -q` | 0 | 33 passed; recurring detector coverage 96.48% | `sha256:b23f89ec7453bab0ffaf60a5c5105a7a86a2c161490aa1c0730d36890c60859a` |
| Critical ledger/cadence command | 0 | 6 passed; repeated-run idempotence, sequential `3 -> 4`, weekly/biweekly/monthly positives, quarterly/yearly mappings and exclusions, reachable biweekly period separation | `sha256:c0989e39fb3568753cc0f14d51126e308b4434f021f1f1eb9cf87dd9b91434f3` |
| Duplicate-rule defensive regression command | 0 | 4 passed; deterministic duplicate survivor, unique-key enforcement, migration dedup, and 0008 downgrade data preservation | `sha256:c2aaf17df58f5db75e019e1457702401af86f4358f89d36939d1c6a299bc16a8` |
| `python -m pytest tests/test_recurring.py tests/test_alembic.py tests/test_ingestion.py::TestRecurringDetectorIntegration -q` | 0 | 56 passed; recurring, migration, and ingestion integration coverage | `sha256:b420d86c73e6674636414949a9d32adb129912ea1ed5933143cf358c98564a9b` |
| `python -m pytest tests/ -q` | 1 | 546 passed, 23 failed; repository coverage 82.81% | `sha256:b177d52cc363c9291b2e74bd7992decfdfedb1e8c62c729f561d1a7b9af9aa8d` |
| `python -m mypy --strict app/` | 1 | 4 errors in 3 unrelated files | `sha256:7a2eea6d43735803db2881cc320b4a36e35329cef0e9f5eab955c70ac46d9790` |
| `python -m ruff check .` | 1 | 2 unrelated errors in `app/cli/seed_demo.py` | `sha256:86cfdad7398260c36dff4f9d0f3cb82b6e2256bbd77de4b4a4b5bff14bd1dbcf` |
| `python -m ruff format --check .` | 1 | 10 unrelated/baseline files would be reformatted | `sha256:aae88c58d0b90356c2a874fe871393c3cc6640f965dd988342425a16c3fe86b4` |
| `python -m ruff check app/services/recurring_detection.py tests/test_recurring.py` | 0 | All checks passed | `sha256:82b3e6a6c090a57601d22943bd23fca9218d1031dbe5a7b754092f9a156b4f18` |
| `python -m ruff format --check app/services/recurring_detection.py tests/test_recurring.py` | 0 | 2 files already formatted | `sha256:3bc53bf3e981a98a34a852e175bf9b77af841edea74fca595d9aedcbaf9a4938` |
| `python -m compileall -q app` | 0 | Passed; exact output was empty | `sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855` |
| `git diff --check` | 0 | Passed; exact output was empty | `sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855` |

Critical ledger/cadence command:

```text
python -m pytest tests/test_recurring.py::TestRecurringDetectorAlgorithm::test_upsert_is_idempotent tests/test_recurring.py::TestRecurringDetectorAlgorithm::test_upsert_counts_only_current_statement_new_occurrences tests/test_recurring.py::TestRecurringDetectorAlgorithm::test_period_classification tests/test_recurring.py::TestRecurringDetectorAlgorithm::test_period_classification_thresholds tests/test_recurring.py::TestRecurringDetectorAlgorithm::test_quarterly_and_yearly_patterns_do_not_fit_90_day_window tests/test_recurring.py::TestRecurringDetectorAlgorithm::test_different_period_creates_separate_rule -q
```

Duplicate-rule defensive regression command:

```text
python -m pytest tests/test_recurring.py::TestRecurringDetectorAlgorithm::test_find_rule_tolerates_duplicate_existing_rows tests/test_alembic.py::test_alembic_seeds_create_unique_upsert_key tests/test_alembic.py::test_alembic_recurring_rules_dedup_on_unique_upgrade tests/test_alembic.py::test_alembic_0008_downgrade_drops_unique_constraint_preserves_data -q
```

### Targeted Correction Confirmation

#### Sequential occurrence ledger and `populate_existing=True`

`RecurringDetector._process_group` increments an existing rule only for in-band rows from the current `statement.id` whose `recurring_rule_id` is not already the selected rule. The production-shaped sequential regression creates a rule from three historical statements and proves the next statement updates the same rule from exactly `3` to `4`, not `6`.

`RecurringDetector._scan_window` explicitly uses `.execution_options(populate_existing=True)`. The same-session idempotence regression creates 3, adds one row to reach 4, and reruns unchanged to remain 4. This runtime path proves the documented refresh requirement prevents stale identity-map FK state from being counted twice.

#### Reachable cadence scenarios under the 90-day window

The detector retains the normative `period_end - 90 days` cutoff. Runtime tests create reachable positive weekly (7-day), biweekly (14-day), and monthly (30-day) patterns entirely inside that window. Helper-level tests preserve `90 -> quarterly` and `365 -> yearly`, while full-detector tests prove three quarterly/yearly occurrences cannot be forced into the 90-day window. The separate-period upsert scenario uses a reachable biweekly pattern.

#### Duplicate-rule defensive behavior

`RecurringDetector._find_rule` no longer assumes at most one pre-existing row. If legacy/concurrent duplicates are returned, it deterministically chooses the same survivor order as migration 0008—highest confidence, then latest `last_seen_date`, then lexicographically smallest UUID—and logs a warning without mutating the duplicate set. The dedicated runtime regression passes.

Migration 0008 remains the storage-level authority: tests prove the five-column unique constraint is enforced, pre-existing duplicate rows are deduplicated before constraint creation, and downgrade removes the constraint while preserving surviving data. These checks pass together with the recurring and ingestion integration suite.

### Unrelated Baseline Failures

The full suite reports the same 23 failures outside the bounded correction. No failure occurs in `tests/test_recurring.py`, `tests/test_alembic.py`, or `TestRecurringDetectorIntegration`.

| Area | Failures | Attribution |
|---|---:|---|
| Config | 1 | Stale CORS default expectation; outside changed recurring files. |
| E2E/ingestion | 6 | Existing chunk-count, merchant-alias, metadata, RUT wrapping, and failed-row persistence expectations; outside the corrected recurring paths. |
| LLM/Zen | 16 | Existing schema/API response-format failures; outside recurring detection. |
| Mypy | 4 errors | `app/cli/seed_demo.py`, `app/services/llm/opencode_zen_client.py`, and `app/web/router.py`; no error in `recurring_detection.py`. |
| Ruff lint | 2 errors | `app/cli/seed_demo.py`; bounded correction files pass. |
| Ruff format | 10 files | None is `app/services/recurring_detection.py` or `tests/test_recurring.py`. |

### Spec Compliance Matrix

| Requirement | Scenario | Passing runtime evidence | Result |
|---|---|---|---|
| End-of-ingest detector | Full-success ingest | `TestRecurringDetectorIntegration::test_detector_logs_info_on_full_success` | ✅ COMPLIANT |
| End-of-ingest detector | Partial-success ingest | `TestRecurringDetectorIntegration::test_detector_logs_warning_on_partial_success` | ✅ COMPLIANT |
| End-of-ingest detector | Detector failure is swallowed | `TestRecurringDetectorIntegration::test_detector_failure_does_not_fail_ingest` | ✅ COMPLIANT |
| End-of-ingest detector | Dedup skips detector | `TestRecurringDetectorIntegration::test_detector_not_called_on_dedup_early_return` | ✅ COMPLIANT |
| 90-day grouped scan | Older occurrences excluded | `test_90_day_window_excludes_older_occurrences` | ✅ COMPLIANT |
| 90-day grouped scan | Installment rows skipped | `test_installment_rows_are_skipped` | ✅ COMPLIANT |
| 90-day grouped scan | Currency groups split | `test_same_merchant_different_currencies_produces_two_rules` | ✅ COMPLIANT |
| Occurrence/tolerance | Two occurrences rejected | `test_two_occurrences_do_not_qualify` | ✅ COMPLIANT |
| Occurrence/tolerance | 30% variance rejected | `test_30_percent_variance_does_not_qualify` | ✅ COMPLIANT |
| Occurrence/tolerance | Three in-band occurrences create rule | `test_15_percent_variance_qualifies` | ✅ COMPLIANT |
| Period classification | Monthly, 30 days | `test_period_classification` | ✅ COMPLIANT |
| Period classification | Weekly, 7 days | `test_period_classification` | ✅ COMPLIANT |
| Period classification | Biweekly, 14 days | `test_period_classification` | ✅ COMPLIANT |
| Period classification | Quarterly threshold mapping, 90 days | `test_period_classification_thresholds` plus `test_quarterly_and_yearly_patterns_do_not_fit_90_day_window` | ✅ COMPLIANT |
| Period classification | Yearly threshold mapping, 365 days | `test_period_classification_thresholds` plus `test_quarterly_and_yearly_patterns_do_not_fit_90_day_window` | ✅ COMPLIANT |
| Confidence | Five identical occurrences yield 1.0 | `test_confidence_saturates_at_five_occurrences` | ✅ COMPLIANT |
| Confidence | Three occurrences yield approximately 0.54 | `test_confidence_formula` | ✅ COMPLIANT |
| Confidence | Ten occurrences yield approximately 0.95 | `test_10_occurrences_5pct_variance_yields_high_confidence` | ✅ COMPLIANT |
| Confidence | Outlier filtered before score | `test_outlier_filtered_by_tolerance_creates_rule` | ✅ COMPLIANT |
| Upsert | First detection creates rule | `test_upsert_is_idempotent` | ✅ COMPLIANT |
| Upsert | Second ingest updates same rule `3 -> 4` | `test_upsert_counts_only_current_statement_new_occurrences` | ✅ COMPLIANT |
| Upsert | Different amount range creates rule | `test_different_amount_range_creates_separate_rule` | ✅ COMPLIANT |
| Upsert | Different in-window period creates rule | `test_different_period_creates_separate_rule` | ✅ COMPLIANT |
| Recurring API | GET excludes inactive rules | `TestRecurringAPI::test_get_recurring_excludes_inactive_and_orders_desc` | ✅ COMPLIANT |
| Recurring API | GET orders by latest date | `TestRecurringAPI::test_get_recurring_excludes_inactive_and_orders_desc` | ✅ COMPLIANT |
| Recurring API | PATCH activates rule | `TestRecurringAPI::test_patch_recurring_activates` | ✅ COMPLIANT |
| Recurring API | PATCH deactivates and preserves FK | `TestRecurringAPI::test_patch_recurring_deactivates_preserves_fk` | ✅ COMPLIANT |

**Compliance summary**: 27/27 scenarios compliant.

### Correctness and Design Coherence

| Area | Status | Notes |
|---|---|---|
| 90-day scan and grouping | ✅ | Filter and grouping match the normative spec. |
| Positive in-window cadence detection | ✅ | Weekly, biweekly, and monthly detector cases pass. |
| Quarterly/yearly mapping | ✅ | Threshold mappings pass without expanding the scan window. |
| Cumulative occurrence update | ✅ | Existing rules increment only by current-statement new in-band rows. |
| Repeated-run idempotence | ✅ | `populate_existing=True` is present and unchanged rerun remains at 4. |
| Sequential-statement ledger | ✅ | Production-shaped test proves exact `3 -> 4`, not `3 -> 6`. |
| Duplicate-rule defense | ✅ | Deterministic survivor path and migration protections pass. |
| D1 confidence rounding | ✅ | Four-decimal Python-side computation remains covered. |
| D2 detector failure isolation | ✅ | Integration test passes. |
| D3 statement-scoped FK backfill | ✅ | Historical audit links are preserved. |
| D4 migration chain | ✅ | 0007 and 0008 migration tests pass. |
| D5 Python confidence arithmetic | ✅ | Implemented and covered. |

### Issues Found

**CRITICAL — change-scoped**: None.

**WARNING — unrelated baseline**:

- The repository-wide suite remains non-green: 546 passed, 23 failed, coverage 82.81%.
- Repository-wide mypy, Ruff lint, and Ruff format checks remain non-green outside the bounded correction.

**WARNING — process/archive**:

- Native `sdd-status` sees two approved terminal authorities and routes archive to `resolve-review`. Restore the correct change-local receipt mirror or remove stale terminal authority through the native review workflow; do not fabricate or hand-edit review metadata.

**SUGGESTION**: Resolve repository baseline failures separately from this verified change.

### Canonical Verification Evidence Preimage

The `evidence_revision` is the SHA-256 of the following exact UTF-8, LF-terminated compact JSON bytes with sorted keys and no trailing spaces:

```json
{"baseline_full_suite_failures":23,"build_command":"python -m compileall -q app","build_exit_code":0,"build_output_hash":"sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855","change":"phase-2-pr5-recurring-detection","change_attributable_blockers":0,"coverage_percent":82.81,"critical_scenario_command":"python -m pytest tests/test_recurring.py::TestRecurringDetectorAlgorithm::test_upsert_is_idempotent tests/test_recurring.py::TestRecurringDetectorAlgorithm::test_upsert_counts_only_current_statement_new_occurrences tests/test_recurring.py::TestRecurringDetectorAlgorithm::test_period_classification tests/test_recurring.py::TestRecurringDetectorAlgorithm::test_period_classification_thresholds tests/test_recurring.py::TestRecurringDetectorAlgorithm::test_quarterly_and_yearly_patterns_do_not_fit_90_day_window tests/test_recurring.py::TestRecurringDetectorAlgorithm::test_different_period_creates_separate_rule -q","critical_scenario_exit_code":0,"critical_scenario_output_hash":"sha256:c0989e39fb3568753cc0f14d51126e308b4434f021f1f1eb9cf87dd9b91434f3","duplicate_rule_command":"python -m pytest tests/test_recurring.py::TestRecurringDetectorAlgorithm::test_find_rule_tolerates_duplicate_existing_rows tests/test_alembic.py::test_alembic_seeds_create_unique_upsert_key tests/test_alembic.py::test_alembic_recurring_rules_dedup_on_unique_upgrade tests/test_alembic.py::test_alembic_0008_downgrade_drops_unique_constraint_preserves_data -q","duplicate_rule_exit_code":0,"duplicate_rule_output_hash":"sha256:c2aaf17df58f5db75e019e1457702401af86f4358f89d36939d1c6a299bc16a8","focused_test_command":"python -m pytest tests/test_recurring.py -q","focused_test_exit_code":0,"focused_test_output_hash":"sha256:b23f89ec7453bab0ffaf60a5c5105a7a86a2c161490aa1c0730d36890c60859a","full_suite_command":"python -m pytest tests/ -q","full_suite_exit_code":1,"full_suite_output_hash":"sha256:b177d52cc363c9291b2e74bd7992decfdfedb1e8c62c729f561d1a7b9af9aa8d","integration_test_command":"python -m pytest tests/test_recurring.py tests/test_alembic.py tests/test_ingestion.py::TestRecurringDetectorIntegration -q","integration_test_exit_code":0,"integration_test_output_hash":"sha256:b420d86c73e6674636414949a9d32adb129912ea1ed5933143cf358c98564a9b","requirements_complete":7,"requirements_total":7,"review_authority_status":"approved","review_gate":"post-apply","review_gate_result":"allow","review_lineage_id":"review-66599bd07a9b3cc8","review_store_revision":"sha256:c72c3ed706492e76bd31e670c3dafb6bc5bd3084f26fecb84d1696112e4fa7a7","scenarios_complete":27,"scenarios_total":27,"test_command":"python -m pytest tests/test_recurring.py tests/test_alembic.py tests/test_ingestion.py::TestRecurringDetectorIntegration -q","test_exit_code":0,"test_output_hash":"sha256:b420d86c73e6674636414949a9d32adb129912ea1ed5933143cf358c98564a9b","verdict":"pass"}
```

### Verdict

**PASS WITH WARNINGS** — all 7 requirements and all 27 scenarios are compliant with current runtime evidence. There are no change-scoped blockers or critical findings. Unrelated repository baseline failures remain, and native archive routing remains blocked by duplicate terminal review authority despite the approved bounded receipt and successful `post-apply` validation for the current lineage.
