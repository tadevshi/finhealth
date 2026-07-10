# Tasks: recurring-info-fixes

## Review Workload Forecast

| Field | Value |
|-------|-------|
| Estimated changed lines | ~200 |
| 400-line budget risk | Low |
| Chained PRs recommended | No |
| Suggested split | single PR |
| Delivery strategy | single-pr |
| Chain strategy | N/A |
| Review budget lines | 800 |

Decision needed before apply: No
Chained PRs recommended: No
Chain strategy: N/A
400-line budget risk: Low

### Suggested Work Units

| Unit | Goal | Likely PR | Notes |
|------|------|-----------|-------|
| 1 | Schema: NaN/inf field_validator on confidence | PR #7 | 1 import + 1 validator + docstring |
| 2 | Migration 0008: UNIQUE constraint with dedup | PR #7 | ~80 LOC new migration + model `__table_args__` |
| 3 | Tests: 5 schema + 2 alembic round-trip | PR #7 | mirror existing patterns in test_recurring.py + test_alembic.py |
| 4 | SDD artifacts (verify-report, apply-progress) | PR #7 | audit trail only |

## Phase 1: Schema Hardening (NaN/inf sanitization)

- [x] 1.1 Add `import math` and `field_validator` to the imports of `app/schemas/domain.py`
- [x] 1.2 Add `@field_validator("confidence", mode="before")` on `RecurringRuleResponse` that returns `0.0` for non-finite floats and passes finite values through unchanged
- [x] 1.3 Update `RecurringRuleResponse` class docstring to document the sanitization contract (NaN / +inf / -inf → 0.0)

## Phase 2: Database Hardening (UNIQUE constraint)

- [x] 2.1 Create `alembic/versions/0008_phase2_recurring_rules_unique.py` with `down_revision = "0007_phase2_recurring_rules"` and module docstring explaining the dedup rationale
- [x] 2.2 Implement `upgrade()`: dedup step (group by 5-tuple, keep row with highest `confidence` and `max(last_seen_date)`, delete others) wrapped in a savepoint, then `op.create_unique_constraint("uq_recurring_rules_upsert_key", "recurring_rules", ["merchant_id", "amount_min", "amount_max", "currency", "period_days"])`
- [x] 2.3 Implement `downgrade()`: `op.drop_constraint("uq_recurring_rules_upsert_key", "recurring_rules", type_="unique")`
- [x] 2.4 Add `UniqueConstraint("merchant_id", "amount_min", "amount_max", "currency", "period_days", name="uq_recurring_rules_upsert_key")` to `RecurringRule.__table_args__` in `app/models/recurring_rule.py`; import `UniqueConstraint` from sqlalchemy
- [x] 2.5 Update model class docstring (lines 70-82 of `app/models/recurring_rule.py`) to mention the UNIQUE constraint alongside the existing `ix_recurring_rules_merchant_currency_period` index

## Phase 3: Test Coverage

- [x] 3.1 Add 5 unit tests to `tests/test_recurring.py`: `test_confidence_nan_coerces_to_zero`, `test_confidence_positive_infinity_coerces_to_zero`, `test_confidence_negative_infinity_coerces_to_zero`, `test_confidence_finite_in_range_passes_through`, `test_confidence_finite_out_of_range_raises` (verifies `Field(ge=0.0, le=1.0)` still enforced)
- [x] 3.2 Add `test_alembic_seeds_create_unique_upsert_key` in `tests/test_alembic.py` mirroring the 0007 round-trip pattern (assert constraint exists with the 5 columns after `upgrade head`)
- [x] 3.3 Add `test_alembic_recurring_rules_dedup_on_unique_upgrade` (seed 3 duplicate rows with confidences 0.7, 0.9, 0.5; run `alembic upgrade head`; assert only the 0.9 row survives and `last_seen_date` is the max across the group)
- [x] 3.4 Run `pytest -q` and confirm all new + existing tests pass; run `ruff check .` and `mypy --strict app/`
- [x] 3.5 *(gate-fix, Round 2 — added in commit `b6ff6fd`)* `test_alembic_0008_downgrade_drops_unique_constraint_preserves_data` closes the coverage gap the verify gate caught: existing round-trip tests downgrade to `base` (which drops the `recurring_rules` table), so they cannot verify the data-preservation half of the "Downgrade drops the unique constraint" spec scenario. This dedicated test downgrades only to `-1` (0008 → 0007), asserts the table still exists, the constraint is gone, the 3-column read-side index is preserved, and the inserted row's data round-trips through `SELECT`.

## Phase 4: SDD Audit Trail

- [x] 4.1 Write `openspec/changes/recurring-info-fixes/verify-report.md` (status PASS, cross-walk the 2 new requirements + their scenarios to implementation + tests, cite pytest output)
- [x] 4.2 Write `openspec/changes/recurring-info-fixes/apply-progress.md` (note Tasks 1-3 complete, commit SHAs, pytest summary)

> **Archive reconciliation (sdd-archive, 2026-07-08)**: All checkboxes were marked `[x]` retroactively by `sdd-archive` per the skill's Task Completion Gate exception clause. The orchestrator explicitly authorized the reconciliation after `apply-progress.md` and `verify-report.md` proved completion: 5 implementation commits on `feat/recurring-info-fixes` (`3a264eb`, `a0ea4fb`, `9f4e205`, `b6ff6fd`, `254f5aa`), pytest 49/49 on the focused run, pytest 348/348 on the full suite, `ruff check .` clean, alembic round-trip clean, verify-report status PASS. No code was changed by this reconciliation. The reason for the reconciliation is recorded in the archive report.
