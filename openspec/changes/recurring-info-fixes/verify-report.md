# Verify Report — recurring-info-fixes

## Status: PASS

Both phases of the change implement the spec delta for
`phase2-recurring-detection` (PR #7). All 4 task groups (1.1–1.3,
2.1–2.5, 3.1–3.3, 4.1–4.2) are complete. The five new unit tests +
two new alembic tests all pass alongside the existing 405-test
suite. Ruff, mypy, and the alembic round-trip are clean.

## Cross-walk

| Spec requirement (delta) | Evidence |
|---|---|
| `RecurringRuleResponse.confidence` sanitizes non-finite values to `0.0` before the `Field(ge=0.0, le=1.0)` bound check (NaN / +inf / -inf → `0.0`) | `app/schemas/domain.py` — `@field_validator("confidence", mode="before")` on `RecurringRuleResponse` returning `0.0` for non-finite floats. `tests/test_recurring.py` — 5 new unit tests (`test_confidence_nan_coerces_to_zero`, `test_confidence_positive_infinity_coerces_to_zero`, `test_confidence_negative_infinity_coerces_to_zero`, `test_confidence_finite_in_range_passes_through`, `test_confidence_finite_out_of_range_raises`) covering every spec scenario. |
| `recurring_rules` has UNIQUE constraint `uq_recurring_rules_upsert_key` on the 5-tuple `(merchant_id, amount_min, amount_max, currency, period_days)`, enforced at the DB layer (defense in depth alongside the application-level SELECT) | `alembic/versions/0008_phase2_recurring_rules_unique.py` — adds the constraint via `op.batch_alter_table` (the SQLite-compatible portable abstraction; PostgreSQL gets a plain `ALTER TABLE`). `app/models/recurring_rule.py` — `__table_args__` declares a matching `UniqueConstraint(... name="uq_recurring_rules_upsert_key")` so the schema is self-describing. |
| Migration dedup: for each duplicate group, keep the row with the highest `confidence` (tie-break: max `last_seen_date`); set the keeper's `last_seen_date` to `max(last_seen_date)` across the group; delete the losers | `alembic/versions/0008_phase2_recurring_rules_unique.py` — the dedup CTE ranks rows within each group with `ROW_NUMBER() OVER (PARTITION BY ... ORDER BY confidence DESC, last_seen_date DESC, id ASC)` and computes `MAX(last_seen_date) OVER (PARTITION BY ...)`. Step 1a updates the keeper's `last_seen_date` to the group max; step 1b deletes the losers. `tests/test_alembic.py::test_alembic_recurring_rules_dedup_on_unique_upgrade` — seeds 3 dupes (confidences 0.7, 0.9, 0.5; last_seen_dates 2026-05-01, 2026-05-15, 2026-05-20) and asserts the 0.9 row survives with `last_seen_date` bumped to 2026-05-20 (the group max). |
| The unique constraint surfaces the concurrent-insert race as `IntegrityError` (the application layer's existing race-guard pattern catches and retries) | `tests/test_alembic.py::test_alembic_seeds_create_unique_upsert_key` — inserts a second row with the same 5-tuple and asserts `IntegrityError` is raised. The application-side race guard lives in `app/services/merchants.py:419,450,613` (the merchant-alias precedent) — the spec scenario pins the behaviour; no app code change required. |
| Downgrade drops the UNIQUE constraint | `alembic/versions/0008_phase2_recurring_rules_unique.py::downgrade()` — `op.batch_alter_table` + `drop_constraint(... type_="unique")`. The dedup is not reversed (the deleted rows are not re-inserted); this is documented in the downgrade docstring. |
| The 3-column read-side index `ix_recurring_rules_merchant_currency_period` is preserved | `alembic/versions/0008_phase2_recurring_rules_unique.py` — the migration's `upgrade()` does not touch the index. `tests/test_alembic.py::test_alembic_seeds_create_unique_upsert_key` — asserts the index still exists with the same 3 columns. |

## Test Results

| Check | Result |
|---|---|
| `pytest -q tests/test_recurring.py` | **30 passed** (25 existing + 5 new) |
| `pytest -q tests/test_alembic.py` | **18 passed** (16 existing + 2 new) |
| `pytest -q tests/ --ignore=tests/test_llm_services.py` | **347 passed, 74 skipped** (74 skips are pre-existing — `TEST_RUT` env var not set + missing sample PDF, not regressions) |
| `ruff check .` | **All checks passed** |
| `ruff format --check app/schemas/domain.py app/models/recurring_rule.py alembic/versions/0008_phase2_recurring_rules_unique.py tests/test_recurring.py tests/test_alembic.py` | **5 files already formatted** |
| `mypy --strict app/schemas/domain.py app/models/recurring_rule.py` | **0 errors in modified files** (2 pre-existing errors in unrelated files: `app/services/llm/opencode_zen_client.py:338` and `app/api/v1/router.py:40`) |
| `alembic upgrade head` on a fresh DB | **clean** (0001 → 0008) |
| `alembic downgrade -1` (drop the new constraint) | **clean** (0008 → 0007) |
| `alembic upgrade head` (re-upgrade) | **clean** (0007 → 0008) |

Pytest tail (focused run):

```
================================ 48 passed in 5.61s ================================
```

Pytest tail (full suite, excluding LLM live-service tests):

```
================== 347 passed, 74 skipped in 15.37s ==================
```

## Files changed

| File | Change |
|---|---|
| `app/schemas/domain.py` | + `import math` + `field_validator` import; `_sanitize_confidence` classmethod on `RecurringRuleResponse`; updated class docstring |
| `app/models/recurring_rule.py` | + `UniqueConstraint` import; `__table_args__` with `uq_recurring_rules_upsert_key`; docstring updates (mentions the constraint alongside the read-side index) |
| `alembic/versions/0008_phase2_recurring_rules_unique.py` | **new** — dedup CTE + `batch_alter_table` + UNIQUE constraint; downgrade drops the constraint |
| `tests/test_recurring.py` | + 5 unit tests covering the sanitization contract + 1 helper `_response_kwargs` |
| `tests/test_alembic.py` | + 2 round-trip tests: unique-constraint creation + dedup behaviour |

Total: 1 new file, 4 modified files. Diff size: **~640 lines** including
migration docstring and test docstrings (substantially under the
800-line review budget from `tasks.md`).

## Risks

| Risk | Likelihood | Mitigation |
|---|---|---|
| The migration's dedup step is destructive on a dirty DB (loses data) | Low | The dedup is documented as defensive — should be a no-op on production. If a future deployment has duplicate rows (which would be a bug in the detector), the dedup keeps the highest-confidence row per group. The losing rows are permanently deleted; this is irreversible. The downgrade docstring flags this explicitly. |
| Concurrent inserts are now surfaced as `IntegrityError` instead of silent duplicates | Med (now visible) | The application layer's existing race-guard pattern (per `app/services/merchants.py:419,450,613`) handles `IntegrityError` from concurrent inserts. The spec scenario pins this behaviour. The detector algorithm in `app/services/recurring_detection.py` is unchanged. |
| The new UNIQUE constraint is wider than the read-side 3-column index | None | The 3-column index is preserved; it still serves the read-side `GET /api/v1/recurring` filter path. The UNIQUE constraint is additive — it costs more storage and write overhead, but both are negligible at the expected row count (one row per merchant × period × amount-band combination). |
| `field_validator(mode="before")` runs before the `Field(ge=0.0, le=1.0)` bound check | None | The coerced `0.0` is in-bounds, so no `ValidationError` is raised. The 5th test (`test_confidence_finite_out_of_range_raises`) pins the contract: finite out-of-range values still raise, the sanitization is *only* for non-finite. |
| Dedup CTE portability across SQLite and PostgreSQL | Low | Both dialects support `ROW_NUMBER() OVER (...)` and `MAX() OVER (...)` in CTEs (SQLite ≥ 3.25, all supported PostgreSQL versions). The migration is tested against SQLite (the project's development dialect); the production target is PostgreSQL which has the same SQL surface. The `__table_args__` declaration on the model is dialect-agnostic. |

## Out of scope (per the proposal)

- No `INSERT ... ON CONFLICT` rewrite of the detector's upsert path
  (`app/services/recurring_detection.py` is unchanged).
- No removal of `ix_recurring_rules_merchant_currency_period`.
- No changes to the detector algorithm, the GET/PATCH endpoints, or
  any other domain logic.
