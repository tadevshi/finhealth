# Tasks: Recurring Detection (Phase 2 PR #5)

## Summary

Total LOC: ~591 | Work units: 7 | PR boundary: single (no chain) | Tests: ~14 new (2 migration + 4 API + 8 algorithm + 4 integration; coverage ≥ 83.71% PR #4 baseline).

## Review Workload Forecast

| Field | Value |
|-------|-------|
| Estimated changed lines | ~591 |
| 400-line budget risk | Low |
| Chained PRs recommended | No |
| Suggested split | single PR |
| Delivery strategy | force-chained (N/A) |
| Chain strategy | N/A |

Decision needed before apply: No
Chained PRs recommended: No
Chain strategy: N/A
400-line budget risk: Low

### Suggested Work Units

| Unit | Goal | Likely PR | Notes |
|------|------|-----------|-------|
| 1 | `RecurringRule` model + migration 0007 + `Transaction.recurring_rule_id` FK | PR #5 | All schema in one chain; tests round-trip the upgrade/downgrade |
| 2 | `RecurringRuleResponse` + `RecurringRuleUpdate` + GET/PATCH endpoints | PR #5 | API independent of detector; tests in `tests/test_recurring.py` |
| 3 | `RecurringDetector` service (90-day scan, ±15%, period, upsert, confidence) | PR #5 | Algorithm tests cover the 7 spec requirements |
| 4 | `ingest_statement` integration (the 6-LOC additive change) | PR #5 | Cherry-pick isolation gate; verify via `git diff main -- app/services/ingestion.py` |

## Phase 1: Foundation (model + migration)

- [x] **1.1** `feat(models): add RecurringRule model + relationship`. Create `app/models/recurring_rule.py` (UUIDMixin + TimestampMixin + Base). `RecurringRule`: `merchant_id` FK→`merchants.id` ON DELETE CASCADE (indexed), `period_days: int`, `period_label: str` ∈ {`weekly`/`biweekly`/`monthly`/`quarterly`/`yearly`} String(16), `amount_min`/`max` Numeric(15,2), `currency` String(3), `is_active: bool` default True, `confidence: float` NOT NULL (D1: 0.0-1.0), `last_seen_date: date`, `occurrences: int`, timestamps. `transactions: Mapped[list[Transaction]] = relationship(back_populates="recurring_rule_ref", lazy="selectin")` (back-ref resolved in 1.3). Re-export from `app/models/__init__.py`. **Files**: `app/models/recurring_rule.py` (new, ~50 LOC), `app/models/__init__.py` (+1). **Acceptance**: `from app.models import RecurringRule` imports. **Deps**: none (`Merchant` from PR #4 is in main).

- [x] **1.2** `feat(migration): add 0007_phase2_recurring_rules`. Create `alembic/versions/0007_phase2_recurring_rules.py` with `down_revision = "0006_phase2_merchants_transactions_alter"` (D4: linear Alembic DAG). Docstring documents why a NEW file (the cherry-pick + PR #2 + PR #4 don't have it; PR #5 keeps 0006 as a 2-PR coordination point). `upgrade()`: `op.create_table("recurring_rules")` + composite index `ix_recurring_rules_merchant_currency_period` on `(merchant_id, currency, period_days)` + `ix_recurring_rules_is_active` on `is_active`; `batch_alter_table("transactions")` adds `recurring_rule_id` column + FK→`recurring_rules.id` ON DELETE SET NULL + index. `downgrade()` reverses in inverse order (transactions first, then new table). Add 2 round-trip tests in `tests/test_alembic.py` (mirror the 0006 round-trip pattern at lines 418-562): `test_alembic_seeds_create_recurring_rules_table` + `test_alembic_transactions_round_trip_recurring_rule_id`. **Files**: `alembic/versions/0007_*.py` (new, ~80), `tests/test_alembic.py` (+50). **Acceptance**: `alembic upgrade head` produces `recurring_rules` + `transactions.recurring_rule_id` FK + indexes; `alembic downgrade -1` reverses; both tests pass. **Deps**: none (schema-only).

- [x] **1.3** `feat(models): add recurring_rule_id FK to Transaction`. In `app/models/transaction.py` after `merchant_id` (line 123), add `recurring_rule_id: Mapped[uuid.UUID | None] = mapped_column(UUIDType(), ForeignKey("recurring_rules.id", ondelete="SET NULL"), nullable=True, index=True)`. After `merchant_ref` (line 142), add `recurring_rule_ref: Mapped["RecurringRule | None"] = relationship(back_populates="transactions", lazy="joined")`. **Files**: `app/models/transaction.py` (+10). **Acceptance**: model imports; round-trip test from 1.2 passes; the `RecurringRule.transactions` back-reference resolves lazily. **Deps**: 1.1 (model must exist), 1.2 (FK column must be designed first).

## Phase 2: Schemas + API

- [x] **2.1** `feat(schemas): add RecurringRuleResponse + RecurringRuleUpdate`. In `app/schemas/domain.py`, add `RecurringRuleResponse` (id, merchant_id, period_label, period_days, amount_min, amount_max, currency, is_active, confidence, last_seen_date, occurrences, created_at, updated_at) with `from_attributes=True`; `confidence: float = Field(..., ge=0.0, le=1.0)` (per D1). Add `RecurringRuleUpdate(is_active: bool)` with `extra="forbid"`. Re-export both from `app/schemas/__init__.py`. **Files**: `app/schemas/domain.py` (+20), `app/schemas/__init__.py` (+2). **Acceptance**: `from app.schemas.domain import RecurringRuleResponse, RecurringRuleUpdate` imports; Pydantic rejects `confidence > 1.0`. **Deps**: none.

- [x] **2.2** `feat(api): add GET /api/v1/recurring + PATCH /api/v1/recurring/{id}`. Create `app/api/v1/recurring.py` (mirror `app/api/v1/merchants.py` pattern). `GET ""` returns `list[RecurringRuleResponse]`, filter `RecurringRule.is_active.is_(True)` (per the `banks.py:69` convention), order `RecurringRule.last_seen_date.desc()`. `PATCH "/{rule_id}"` accepts `RecurringRuleUpdate` body, sets `is_active` (200 with updated rule, 404 if not found), atomic single `commit()`; per design D, does NOT clear `recurring_rule_id` on existing transactions. Register `recurring_router` in `app/api/v1/router.py` between `merchants_router` and `statements_router` (PR-adjacency). Add 4 API tests in `tests/test_recurring.py`: GET excludes inactive + orders desc, PATCH activate (200), PATCH deactivate (200 + rule excluded from subsequent GET + historical FK preserved), PATCH 404. **Files**: `app/api/v1/recurring.py` (new, ~70), `app/api/v1/router.py` (+2), `tests/test_recurring.py` (new, +30 for API). **Acceptance**: endpoints respond per spec R7; router registered. **Deps**: 1.1, 2.1.

## Phase 3: Core Service

- [x] **3.1** `feat(services): add RecurringDetector with 90-day scan + upsert + confidence + period classification`. Create `app/services/recurring_detection.py`. `class RecurringDetector(session: AsyncSession, partial_success: bool)`. `async def detect(self, statement: Statement) -> list[RecurringRule]`. Algorithm: (1) one-query scan — same `credit_card_id`, `date >= statement.period_end - timedelta(days=90)`, `installment_number IS NULL`, `merchant_id IS NOT NULL`; (2) group by `(credit_card_id, merchant_id, currency)` in Python; (3) per group with ≥3 in-band — compute median amount → ±15% band → filter outliers → median interval between consecutive dates → classify period (design B: weekly ≤10d, biweekly ≤18d, monthly ≤45d, quarterly ≤120d, yearly ≤400d); (4) upsert by `(merchant_id, amount_min, amount_max, currency, period_days)` (design D: ignores `is_active` — `SELECT` on the composite key, UPDATE on hit / INSERT on miss); (5) `confidence = round(min(1.0, occurrences/5) * max(0.0, 1.0 - (amount_max-amount_min)/median_amount), 4)` (D1, D5, decision #10, Python-side arithmetic); (6) FK backfill (D3: in-statement rows where `merchant_id == rule.merchant_id AND amount >= amount_min AND amount <= amount_max AND currency == rule.currency`); (7) commit. Log per decision #7: `logger.info("Recurring detection complete for statement=%s: %d rules")` on full-success, `logger.warning("...%d rules (partial-success ingest)")` on partial-success. Add 8 algorithm tests in `tests/test_recurring.py`: 90-day window filter, ≥3 occurrences threshold, ±15% amount tolerance (incl. outlier exclusion), installment skip, period classification (weekly/biweekly/monthly/quarterly/yearly), idempotent upsert (re-run does not duplicate), confidence rounding (D1: 4 decimals, e.g. `0.6 * 0.905 = 0.543`), empty result (no patterns). **Files**: `app/services/recurring_detection.py` (new, ~140), `tests/test_recurring.py` (new, +90 for detector). **Acceptance**: all 8 algorithm tests pass against ... (line truncated to 2000 chars)

## Phase 4: Integration

- [x] **4.1** `feat(ingestion): wire RecurringDetector into ingest_statement`. In `app/services/ingestion.py`: (a) module-level import in the existing imports block (after line 72): `from app.services.recurring_detection import RecurringDetector`; (b) `__init__` body (after line 164): add `self._last_failed_chunks = 0` (defensive init); (c) `_run_chunked_extraction` (after line 571 `deduped = _dedupe_transactions(...)`, before the `return ExtractionResponse(` at line 573): add `self._last_failed_chunks = failed_chunks` (1 additive line, **NO flow change**, the loop is untouched — **KNOWN RISK #1**); (d) `ingest_statement` (after the `logger.info` at line 420, before `return statement` at line 421): wrap the call in bare `try/except Exception` (D2: catches everything, `logger.exception(...)` with statement id, swallow) — `await RecurringDetector(self._session, partial_success=self._last_failed_chunks > 0).detect(statement)`. The dedup early return at line 356 stays BEFORE the detector call (design E). The chunk loop (lines 483-585), `try/finally`, `first_successful_chunk_seen`, `last_chunk_exc`, all-fail guard, metadata-None guard, counters, `_metadata_completeness` are all UNTOUCHED. Add 4 integration tests in `tests/test_ingestion.py`: full-success → `logger.info` (caplog), partial-success → `logger.warning` (caplog), detector failure → does not fail ingest (try/except wrapper), detector not called on dedup early return at line 356. **Files**: `app/services/ingestion.py` (+6), `tests/test_ingestion.py` (+40). **Acceptance**: `git diff main..feat/phase2-pr5-recurring-detection -- app/services/ingestion.py` shows ~6 LOC additive only (import + `__init__` + 1-line stash + detector call wrapper); all 4 integration tests pass. **Deps**: 3.1.

## Cherry-Pick Isolation Gate (apply verifies via `git diff`)

- `app/services/ingestion.py:483-585` (chunk loop + return): **ZERO changes** to loop, `try/finally`, `first_successful_chunk_seen`, `last_chunk_exc`, all-fail guard, metadata-None guard, counters, `_metadata_completeness` ✓
- `app/services/ingestion.py` after line 571 (just before `return ExtractionResponse`): **+1 LOC** stash `self._last_failed_chunks = failed_chunks` — **KNOWN RISK #1**, additive only, call out in PR description ✓
- `app/services/llm/{prompts,schemas,*_client}.py`: **ZERO changes** ✓
- `app/services/merchants.py`, `app/web/router.py`, `app/core/config.py`: **ZERO changes** ✓
- Verify: `git diff main..feat/phase2-pr5-recurring-detection -- app/services/ingestion.py` shows ~6 LOC additive (import + `__init__` + stash + detector call wrapper).

## Constraints

- NEW migration file `0007` (do NOT extend 0006). NO new column on `categories` or `merchants`.
- `transactions.recurring_rule_id` is a NEW nullable column (SET NULL on delete).
- NO change to LLM prompt, clients, or schemas. NO change to chunk loop / `try/finally` / counters / `_metadata_completeness`.
- The ONLY change to `ingestion.py` is the additive 6-LOC block.
- DO NOT add `Co-Authored-By` to commits. DO NOT skip tests or lint/type gates.
- DO NOT push to `main` directly. The PR is the integration path.
