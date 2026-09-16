# Design: Recurring Detection (Phase 2 PR #5)

## Context

Finhealth extracts every line-item from bank statements, but the user must scan rows by hand to spot subscriptions and utility bills. PR #5 introduces a deterministic recurring-transaction detector at the end of every successful `ingest_statement`, plus two endpoints for review.

PR #2 (Categories), PR #3 (Categories UI), PR #4 (Merchants + Aliases) are merged in main. PR #5 builds on PR #4's `merchant_id` FK (grouping key) and PR #2's `installment_number` (skip filter). The 4 product decisions (#3 sync, #7 partial-success, #10 confidence, #14 log-only) are locked in engram `sdd/finhealth-phase2/decisions`.

The cherry-pick isolation gate is the strictest of the 5-PR plan: the diff to `app/services/ingestion.py` is ~6 LOC additive. LLM prompts/clients/schemas, the chunk loop, and the web router are untouched.

## Goals & Non-Goals

**Goals**: detect ≥3-occurrence patterns (90-day, ±15%); classify cadence; score `confidence` 0.0-1.0; expose `GET /api/v1/recurring` + `PATCH /api/v1/recurring/{id}`; run sync; swallow failures.

**Non-Goals**: `deactivated_at`; recurring UI; LLM rules; Sentry; PR #6.

## Approach

### Data Model + Migration 0007

`RecurringRule` (`app/models/recurring_rule.py`, new, ~50 LOC) follows `UUIDMixin`+`TimestampMixin`. Columns: `merchant_id` (FK→`merchants.id` CASCADE, indexed), `period_days`, `period_label` String(16), `amount_min`/`max` Numeric(15,2), `currency` String(3), `is_active`, `confidence` Real 0.0-1.0, `last_seen_date`, `occurrences`, timestamps. Composite index `(merchant_id, currency, period_days)`; B-tree on `is_active`. `Transaction` gains `recurring_rule_id` FK→`recurring_rules.id` SET NULL, indexed (+10 LOC). **Migration 0007** (new, ~80 LOC). D4: `down_revision = "0006_phase2_merchants_transactions_alter"`. Creates `recurring_rules` + 2 indexes → `ALTER TABLE transactions ADD recurring_rule_id` + FK + index. Downgrade reverses.

### RecurringDetector

`app/services/recurring_detection.py` (new, ~140 LOC). `class RecurringDetector(session, partial_success)`. Single-query scan: same `credit_card_id`, `date >= period_end - 90d`, `installment_number IS NULL`, `merchant_id IS NOT NULL`. Group by `(merchant_id, currency)`. Per group: ≥3 in-band; median amount; ±15% band; filter outliers; median interval; classify `period_days` (B: weekly ≤10d, biweekly ≤18d, monthly ≤45d, quarterly ≤120d, yearly ≤400d). Upsert by `(merchant_id, amount_min, amount_max, currency, period_days)` (D: ignores `is_active`). Confidence (D5, Python-side, decision #10): `round(min(1.0, occurrences/5) * max(0.0, 1.0 - (amount_max-amount_min)/median_amount), 4)` (D1). FK backfill (D3). Log diff'd (decisions #7, #14).

### Ingestion Integration (~6 LOC, cherry-pick gate)

`app/services/ingestion.py`:
- **Import** (+1): `from app.services.recurring_detection import RecurringDetector`
- **`__init__`** (+1): `self._last_failed_chunks = 0`
- **`_run_chunked_extraction` line 578** (+1 additive, **NO flow change**): `self._last_failed_chunks = failed_chunks` before `return`. **KNOWN RISK #1**: inside cherry-pick-protected function. Additive only; stash unavoidable per decision #7. PR description must call this out.
- **`ingest_statement` after line 415's `refresh`** (~3-4 LOC, D2):
  ```python
  try:
      await RecurringDetector(self._session, partial_success=self._last_failed_chunks > 0).detect(statement)
  except Exception:
      logger.exception("Recurring detection failed for statement=%s", statement.id)
  ```

Dedup early return at line 356 stays BEFORE detector call (E).

### API Endpoints

`app/api/v1/recurring.py` (new, ~70 LOC):
- **`GET /api/v1/recurring`** — filter `is_active=True`, order `last_seen_date` desc, returns `list[RecurringRuleResponse]`
- **`PATCH /api/v1/recurring/{id}`** — `RecurringRuleUpdate(is_active: bool)`, 404 if not found. FK on historical txns preserved (D)

## Decisions

| # | Decision | Choice | Rationale |
|---|----------|--------|-----------|
| D1 | Confidence rounding | `round(confidence, 4)` | Clean, deterministic, 4-decimal precision enough. |
| D2 | Detector wrapper | Bare `try/except Exception` | Spec: failure does not fail ingest — must catch all. |
| D3 | FK backfill | In-statement rows matching upsert key | Amount check ensures FK only on in-band. |
| D4 | Migration chain | `down_revision = "0006_phase2_merchants_transactions_alter"` | Linear DAG. 0006 revision ID is stable. |
| D5 | Confidence arithmetic | Python-side in detector | Simple, easy to test. |

## File Changes

| File | Action | LOC |
|------|--------|-----|
| `app/models/recurring_rule.py` | Create | ~50 |
| `alembic/versions/0007_phase2_recurring_rules.py` | Create | ~80 |
| `app/services/recurring_detection.py` | Create | ~140 |
| `app/api/v1/recurring.py` | Create | ~70 |
| `tests/test_recurring.py` | Create | ~120 |
| `app/models/transaction.py` | Modify | +10 |
| `app/services/ingestion.py` | Modify | +6 |
| `app/schemas/domain.py` | Modify | +20 |
| `tests/test_alembic.py` | Modify | +50 |
| `tests/test_ingestion.py` | Modify | +40 |
| 3 init/router | Modify | +5 |

**Total**: ~591 LOC (5 new, 8 modified). Under 800-line budget.

## Test Strategy

**14 new tests** (coverage ≥ 83.17%):
- **Detection (12)**: window, threshold, tolerance, installment skip, period (5), idempotency, deactivation, partial-success log, empty result, FK backfill, confidence rounding
- **Migration (2)**: round-trip
- **Ingestion (4)**: full/partial-success logs, failure swallowed, dedup bypass

## Migration & Rollback

**NEW migration** `0007_phase2_recurring_rules.py` (chains off 0006). Pure schema: 1 table + 1 nullable column. **Rollback**: revert the commit. `recurring_rule_id` is nullable; `RecurringDetector` removed. No data loss.

## Cherry-Pick Isolation Gate

| File | Constraint |
|------|-----------|
| `ingestion.py:483-578` (chunk loop) | **ZERO changes** |
| `ingestion.py:578` (before `return`) | **+1 LOC** — **KNOWN RISK #1**; call out in PR |
| `llm/prompts.py`, `llm/schemas.py`, `llm/*_client.py` | **ZERO changes** |
| `services/merchants.py`, `web/router.py`, `core/config.py` | **ZERO changes** |

Apply verifies `git diff main -- app/services/ingestion.py` shows only ~6 LOC.

## Out of Scope

`deactivated_at`; recurring UI; PR #6; LLM installment; LLM rules; Sentry.
