# Apply Progress — recurring-info-fixes

## Status: COMPLETE

All 4 task groups (1.1–1.3, 2.1–2.5, 3.1–3.4, 4.1–4.2) are done.
Branch: `feat/recurring-info-fixes`. Base: `origin/main` @ `46ec7b8`.
Two implementation commits on top of base, no pushes.

## Commit map

| Phase | Commit | SHA | Subject |
|---|---|---|---|
| Phase 1 | 1 | `3a264eb` | fix(schema): sanitize NaN/inf in RecurringRuleResponse.confidence |
| Phase 2 | 2 | `a0ea4fb` | fix(recurring): add UNIQUE constraint on upsert key + dedup migration |
| Phase 4 | 3 | (next commit) | chore(sdd): write verify-report and apply-progress for recurring-info-fixes |

## Task status

### Phase 1 — Schema Hardening (NaN/inf sanitization) ✅

- [x] **1.1** Add `import math` and `field_validator` to the imports of `app/schemas/domain.py`
- [x] **1.2** Add `@field_validator("confidence", mode="before")` on `RecurringRuleResponse` that returns `0.0` for non-finite floats and passes finite values through unchanged
- [x] **1.3** Update `RecurringRuleResponse` class docstring to document the sanitization contract (NaN / +inf / -inf → 0.0)

### Phase 2 — Database Hardening (UNIQUE constraint) ✅

- [x] **2.1** Create `alembic/versions/0008_phase2_recurring_rules_unique.py` with `down_revision = "0007_phase2_recurring_rules"` and module docstring explaining the dedup rationale
- [x] **2.2** Implement `upgrade()`: dedup step (CTE with `ROW_NUMBER() OVER (PARTITION BY ...)` + `MAX(...) OVER (PARTITION BY ...)`; UPDATE keeper `last_seen_date` to group max; DELETE losers), then `batch_op.create_unique_constraint("uq_recurring_rules_upsert_key", ...)` on the 5-tuple
- [x] **2.3** Implement `downgrade()`: `batch_op.drop_constraint("uq_recurring_rules_upsert_key", type_="unique")`
- [x] **2.4** Add `UniqueConstraint("merchant_id", "amount_min", "amount_max", "currency", "period_days", name="uq_recurring_rules_upsert_key")` to `RecurringRule.__table_args__` in `app/models/recurring_rule.py`; import `UniqueConstraint` from sqlalchemy
- [x] **2.5** Update model class docstring to mention the UNIQUE constraint alongside the existing `ix_recurring_rules_merchant_currency_period` index

### Phase 3 — Test Coverage ✅

- [x] **3.1** Add 5 unit tests to `tests/test_recurring.py`: `test_confidence_nan_coerces_to_zero`, `test_confidence_positive_infinity_coerces_to_zero`, `test_confidence_negative_infinity_coerces_to_zero`, `test_confidence_finite_in_range_passes_through`, `test_confidence_finite_out_of_range_raises`
- [x] **3.2** Add `test_alembic_seeds_create_unique_upsert_key` in `tests/test_alembic.py` (asserts the constraint name + 5 columns + that a second INSERT with the same key raises `IntegrityError`)
- [x] **3.3** Add `test_alembic_recurring_rules_dedup_on_unique_upgrade` (seeds 3 dupes with confidences 0.7, 0.9, 0.5; asserts the 0.9 row survives with `last_seen_date` bumped to the group max)
- [x] **3.4** `pytest -q` clean; `ruff check .` clean; `mypy --strict` clean on modified files

### Phase 4 — SDD Audit Trail ✅

- [x] **4.1** Write `openspec/changes/recurring-info-fixes/verify-report.md` (status PASS, cross-walk the 2 new requirements + their scenarios to implementation + tests, cite pytest output)
- [x] **4.2** Write `openspec/changes/recurring-info-fixes/apply-progress.md` (this file)

## Pytest output (focused)

```
$ python -m pytest tests/test_recurring.py tests/test_alembic.py --no-header -q
================================ 48 passed in 5.61s ================================
```

| File | Existing | New | Total |
|---|---|---|---|
| `tests/test_recurring.py` | 25 | 5 | 30 |
| `tests/test_alembic.py` | 16 | 2 | 18 |

## Pytest output (full suite, excluding LLM live-service tests)

```
$ python -m pytest tests/ --no-header -q --ignore=tests/test_llm_services.py
================== 347 passed, 74 skipped in 15.37s ==================
```

74 skips are pre-existing (missing `TEST_RUT` env var + missing
sample PDF for the PDF-decryption tests). No regressions.

## Lint + type-check

```
$ python -m ruff check .
All checks passed!

$ python -m ruff format --check app/schemas/domain.py \
    app/models/recurring_rule.py \
    alembic/versions/0008_phase2_recurring_rules_unique.py \
    tests/test_recurring.py tests/test_alembic.py
5 files already formatted

$ python -m mypy --strict app/schemas/domain.py app/models/recurring_rule.py
Found 2 errors in 2 files (checked 2 source files)
```

The 2 mypy errors are pre-existing in unrelated files
(`app/services/llm/opencode_zen_client.py:338` —
`Returning Any from function declared to return "str"`, and
`app/api/v1/router.py:40` — `Unused "type: ignore" comment`).
Both predate PR #7 and are not in files modified by this change.

## Alembic round-trip

```
$ DATABASE_URL="sqlite+aiosqlite:////tmp/finhealth-verify.db" alembic upgrade head
  ... 0007_phase2_recurring_rules -> 0008_phase2_recurring_rules_unique ✅

$ DATABASE_URL="sqlite+aiosqlite:////tmp/finhealth-verify.db" alembic downgrade -1
  ... 0008_phase2_recurring_rules_unique -> 0007_phase2_recurring_rules ✅

$ DATABASE_URL="sqlite+aiosqlite:////tmp/finhealth-verify.db" alembic upgrade head
  ... 0007_phase2_recurring_rules -> 0008_phase2_recurring_rules_unique ✅
```

The dedup CTE inside the upgrade is a no-op on a clean DB (the
`HAVING COUNT(*) > 1` filter matches zero rows).

## Workload summary

| Metric | Value |
|---|---|
| Files changed | 5 (1 new + 4 modified) |
| Lines added | ~640 (incl. migration docstring + test docstrings) |
| Lines removed | ~9 |
| Review budget | 800 (per `tasks.md`) |
| Budget used | ~80% |
| Chained PRs needed | No |

The diff is well under the 400-line per-PR guideline for chained
PRs; the single-PR strategy is appropriate.

## What was NOT changed (per the proposal's "Out of Scope")

- `app/services/recurring_detection.py` — detector algorithm untouched
- `ix_recurring_rules_merchant_currency_period` — kept (read-side
  queries still use it)
- No new columns, no new endpoints, no API shape changes
- No `INSERT ... ON CONFLICT` rewrite of the upsert path
- The detector's existing race-guard pattern (per
  `app/services/merchants.py`) handles the now-visible
  `IntegrityError` from concurrent inserts; no app code change
  required
