# Proposal: Recurring Detection — Defensive Hardening (NaN/inf + Unique Constraint)

## Intent

Close the 2 INFO theoretical findings deferred from PR #5
(`phase-2-recurring-detection`) judgment-day Round 2. Both harden the
existing `phase2-recurring-detection` capability against future regressions
without changing any user-visible behavior.

1. **NaN/inf defense** — `RecurringRuleResponse.confidence` is bounded
   `Field(ge=0.0, le=1.0)`. A future bug that produced `NaN` / `inf` (e.g.
   division by zero in a refactored `_compute_confidence`) would raise
   `ValidationError` on every API response until the bad row is fixed.
2. **Unique constraint on upsert key** — the detector's SELECT-then-INSERT
   path is not atomic. Two concurrent detector runs on the same pattern
   can both miss the existing row, both INSERT, and create duplicates.
   The composite index `ix_recurring_rules_merchant_currency_period`
   covers only 3 of the 5 upsert-key columns and is non-unique.

## Scope

### In Scope
- `app/schemas/domain.py` — `field_validator("confidence", mode="before")` on `RecurringRuleResponse` that sanitizes non-finite floats to `0.0`
- `alembic/versions/0008_phase2_recurring_rules_unique.py` — new migration adding `UNIQUE` constraint on `(merchant_id, amount_min, amount_max, currency, period_days)`, with a dedup step in `upgrade()` if existing data has duplicates
- `app/models/recurring_rule.py` — document the new constraint on the `RecurringRule` class docstring (replace the "composite index" mention with "composite UNIQUE constraint")
- `tests/test_recurring.py` — tests for NaN/inf sanitization (3 cases) and unique-constraint enforcement (1 round-trip case)

### Out of Scope
- No new features, schema columns, or API endpoints
- No removal of `ix_recurring_rules_merchant_currency_period` (kept — still useful for read-side queries that filter on those 3 columns)
- No `INSERT ... ON CONFLICT` rewrite of the upsert (SELECT-then-INSERT is unchanged; the constraint surfaces the race as `IntegrityError` instead of hiding it as silent duplicates)

## Capabilities

### New Capabilities
None.

### Modified Capabilities
- `phase2-recurring-detection`: add a requirement that `RecurringRuleResponse.confidence` rejects non-finite values at the schema layer (NaN/inf → `0.0`), and a requirement that the `recurring_rules` table has a `UNIQUE` constraint on the 5-column upsert key, enforced at the DB layer (defense in depth alongside the application-level SELECT).

## Approach

**Validator (Pydantic v2)**: import `field_validator` from `pydantic` at the top of `app/schemas/domain.py`. Add a method to `RecurringRuleResponse`:

```python
@field_validator("confidence", mode="before")
@classmethod
def _sanitize_confidence(cls, v: float) -> float:
    if isinstance(v, float) and not math.isfinite(v):
        return 0.0
    return v
```

Add `import math` at module top. Test cases: `float("nan")` → `0.0`, `float("inf")` → `0.0`, `float("-inf")` → `0.0`, `0.5` → `0.5` (passthrough), ORM-bound row with a `NaN` value → `0.0`.

**Migration**: `down_revision = "0007_phase2_recurring_rules"`. `upgrade()`:

1. **Dedup step** (defensive — runs even if the table is currently clean): `SELECT merchant_id, amount_min, amount_max, currency, period_days, COUNT(*)` grouped, find groups with `COUNT > 1`, keep the row with the highest `confidence` per group, delete the rest. Wrap in a savepoint so a clean DB still succeeds.
2. `op.create_unique_constraint("uq_recurring_rules_upsert_key", "recurring_rules", ["merchant_id", "amount_min", "amount_max", "currency", "period_days"])`.
3. `downgrade()`: `op.drop_constraint("uq_recurring_rules_upsert_key", "recurring_rules", type_="unique")`.

The existing `ix_recurring_rules_merchant_currency_period` is **not** dropped — the new UNIQUE constraint serves the upsert path; the 3-col index still serves read-side queries that filter on `(merchant_id, currency, period_days)` alone.

## Affected Areas

| Area | Impact | Description |
|------|--------|-------------|
| `app/schemas/domain.py` | Modified | +1 import (`math`, `field_validator`), +1 validator method on `RecurringRuleResponse` |
| `alembic/versions/0008_*.py` | New | Dedup + UNIQUE constraint + downgrade (~60 LOC) |
| `app/models/recurring_rule.py` | Modified | Docstring update: "composite UNIQUE constraint" replaces "composite index" (semantic, no code) |
| `tests/test_recurring.py` | Modified | +3 NaN/inf cases, +1 unique-constraint round-trip |

## Risks

| Risk | Likelihood | Mitigation |
|------|------------|------------|
| Migration fails on existing duplicates | Low | Dedup step in `upgrade()` keeps the highest-confidence row per group |
| Race in upsert path: SELECT-then-INSERT is not atomic; new constraint surfaces it as `IntegrityError` | Med (now visible) | `IntegrityError` is already caught in `app/services/merchants.py:419,450,613` (the merchant-alias race guard precedent). Same pattern applies here. |
| `field_validator(mode="before")` runs before Pydantic's `ge`/`le` check — `0.0` is in-bounds so no regression | Low | `0.0` satisfies `ge=0.0, le=1.0`; validator is the only path for non-finite values |
| Migration on PostgreSQL vs SQLite: UNIQUE constraint syntax is portable | Low | Alembic `op.create_unique_constraint` is the portable abstraction (used in 0006) |

## Rollback Plan

Revert the commit. The migration's `downgrade()` drops `uq_recurring_rules_upsert_key`. The validator on `RecurringRuleResponse` is removed in the same revert. No data loss — the constraint is additive, and any rows that were duplicates were already eliminated by the dedup step in `upgrade()` (their absence is the only change).

## Dependencies

- PR #5 (`phase-2-recurring-detection`) merged at `46ec7b8` (migration 0007 is the `down_revision` of the new 0008)

## Success Criteria

- [ ] `RecurringRuleResponse(confidence=float("nan"))` returns `confidence=0.0` instead of raising `ValidationError`
- [ ] `RecurringRuleResponse(confidence=float("inf"))` and `float("-inf")` return `confidence=0.0`
- [ ] Migration 0008 applies cleanly on a fresh DB AND on the current production DB
- [ ] `downgrade()` reverses cleanly (constraint dropped, no orphan indexes)
- [ ] New unique constraint is enforced: a second `INSERT` with the same 5-tuple raises `IntegrityError` at flush
- [ ] All existing 405 tests still pass; +4 new tests
- [ ] `ruff check .` and `mypy --strict app/` clean on modified files
- [ ] Coverage ≥ 83.17% (PR #2 baseline) maintained
