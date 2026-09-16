# Proposal: Recurring Detection (Phase 2 PR #5)

## Intent

Finhealth detects every line-item in bank statements, but users must manually spot subscriptions,
utility bills, and repeat charges row by row. This change introduces a deterministic recurring-transaction
detector that runs at the end of each successful ingest — identifying subscriptions by merchant,
amount, and cadence — and exposes an API for the user to review and override detected rules.

## Scope

### In Scope
- `RecurringRule` model + migration 0007 (new table: `recurring_rules` + `transactions.recurring_rule_id` FK)
- `RecurringDetector` service (~140 LOC): scan 90d, group by `(merchant_id, currency)`, ≥3 occurrences, ±15% amount tolerance, period classification, upsert rule, set FK on matched transactions
- Ingestion integration (~6 LOC in `ingestion.py`): defensive init + stash `failed_chunks` + detector call wrapped in `try/except`
- `GET /api/v1/recurring` (filter `is_active=True`, order `last_seen_date` desc) + `PATCH /api/v1/recurring/{id}` (`is_active` override)
- `RecurringRuleResponse` + `RecurringRuleUpdate` schemas (~20 LOC)
- Tests (~150 LOC): 8–10 detection tests, 2 migration round-trip tests, 4 ingestion integration tests

### Out of Scope
- `RecurringRule.deactivated_at` timestamp column — historical FK is preserved on deactivation (design D)
- Recurring override UI — per-item PATCH is the override path (decision #12)
- Phase 2 PR #6 (Docs + e2e)
- LLM installment detection improvements — deterministic filter is the v1 path
- Recurring rule generation via LLM — algorithm is deterministic + statistical
- Sentry alerts — log-only per decision #14

## Capabilities

### New Capabilities
- **phase2-recurring-detection**: Deterministic recurring-transaction detection via the
  `RecurringDetector` service at end-of-ingest, with `confidence` scoring, `is_active` override,
  and API list + PATCH. Elaborates the stub from PR #2's archive.

### Modified Capabilities
None. PR #5 is additive; `ingestion-chunk-failure-tolerance`, `phase2-categories`, and
`phase2-merchant-aliasing` are unchanged.

## Approach

**5 design choices (from explore, user-approved)**:

| Choice | Decision |
|--------|----------|
| A — `partial_success` flag | Service-instance attr `_last_failed_chunks` set in `_run_chunked_extraction` before return (1 additive line) |
| B — Period thresholds | weekly ≤10d, biweekly ≤18d, monthly ≤45d, quarterly ≤120d, yearly ≤400d |
| C — Confidence formula | `min(1.0, occurrences / 5) × max(0.0, 1.0 − (max − min) / median)` (decision #10) |
| D — Rule deactivation | Keep FK on historical transactions; upsert key ignores `is_active` so no duplicate rules |
| E — Trigger condition | Run on every successful ingest NOT on dedup path (dedup returns at line 356 before detector) |

**Product decisions** (#3: sync at end of ingest; #7: always run, log differentiated; #10: explicit `confidence` column; #14: log only).

**File-by-file**: 5 new files (model, migration, service, API router, test), 8 modified files (transaction model, models `__init__`, ingestion service, schemas, schemas `__init__`, API router, 2 test files). Total ~350 LOC — well under the 800-line D2 budget.

## Affected Areas

| Area | Impact | Description |
|------|--------|-------------|
| `app/models/recurring_rule.py` | New | `RecurringRule` model (~50 LOC) |
| `app/models/transaction.py` | Modified | Add `recurring_rule_id` FK + relationship (~10 LOC) |
| `alembic/versions/0007_*.py` | New | `recurring_rules` table + FK + indexes (~80 LOC) |
| `app/services/recurring_detection.py` | New | `RecurringDetector` service (~140 LOC) |
| `app/services/ingestion.py` | Modified | Detector call + `partial_success` stash (~6 LOC, cherry-pick isolation gate) |
| `app/api/v1/recurring.py` | New | GET + PATCH endpoints (~70 LOC) |
| `app/schemas/domain.py` | Modified | `RecurringRuleResponse` + `RecurringRuleUpdate` (~20 LOC) |

## Risks

| Risk | Likelihood | Mitigation |
|------|------------|------------|
| Cherry-pick gate: `_last_failed_chunks` stash is additive but inside the protected `_run_chunked_extraction` function | Med | No flow change, no counter change, no exception change. Called out in PR description for reviewer |
| Installment false-positives: LLM misses `installment_number` marker → detector sees partial monthly charge | Low | `installment_number IS NULL` filter; future LLM improvement out of scope |
| Variable utility bills pass ±15% tolerance → false-positive rule | Low | Low `confidence` score (amount-consistency penalty); user can PATCH off |
| Migration 0007 chain: `down_revision = "0006"` must remain valid after rebase | Low | Linear Alembic DAG; 0006 revision ID is stable in main |
| Rule deactivation + re-detection: upsert key ignores `is_active` → always updates same row | Low | No duplicate rules; PATCH flips flag, upsert updates it |

## Rollback Plan

Revert the commit. The migration downgrade drops `transactions.recurring_rule_id` column + index,
then drops `recurring_rules` table. The `RecurringDetector` service is removed. No data loss —
`recurring_rule_id` is nullable and the service is additive.

## Dependencies

- PR #2 (Categories Foundation) — `installment_number`/`installment_total` columns (installment-skip filter input)
- PR #3 (Categories UI) — per-row PATCH pattern (cherry-pick isolation gate precedent)
- PR #4 (Merchants + aliases) — `merchant_id` FK on transactions (detector's grouping key)
- All three are merged in main at `d44c204`

## Success Criteria

- [ ] Migration 0007 lands cleanly (upgrade + downgrade round-trip)
- [ ] 14 new tests pass (8–10 detection + 2 migration + 4 integration)
- [ ] Cherry-pick isolation holds: `git diff main -- app/services/ingestion.py` shows ~6 LOC additive only
- [ ] Detector runs on every successful ingest; dedup path is a no-op
- [ ] Coverage ≥ 83.17% (PR #2 baseline); ruff + mypy clean
- [ ] `GET /api/v1/recurring` returns active rules ordered by `last_seen_date`; `PATCH` returns 404 on unknown id
