# Archive Report — recurring-info-fixes

## Status: READY FOR PR

## Change Summary
- **Change name**: `recurring-info-fixes`
- **Branch**: `feat/recurring-info-fixes`
- **Base**: `origin/main` @ `46ec7b8` (PR #34 merged — Phase 2 PR #6 docs+e2e)
- **Commits**: 5 (3 implementation + 1 gate-correction fix + 1 audit-trail append)
- **Total diff**: 1434 lines (768 code+tests + 666 SDD artifacts)
- **Review budget**: 800 lines (exceeded by 79% in total, but code+tests is 768 lines — within reason given the 10 spec scenarios needing distinct coverage)

## What Was Delivered
- `app/schemas/domain.py` — `field_validator("confidence", mode="before")` on `RecurringRuleResponse` sanitizes NaN/+inf/-inf to 0.0
- `app/models/recurring_rule.py` — `__table_args__` with `UniqueConstraint(merchant_id, amount_min, amount_max, currency, period_days, name="uq_recurring_rules_upsert_key")`
- `alembic/versions/0008_phase2_recurring_rules_unique.py` — new migration: dedup in upgrade (CTE with `ROW_NUMBER`, keeper = max confidence, tie-break = max `last_seen_date`, then min id; the keeper's `last_seen_date` is bumped to the group max), then `op.batch_alter_table` + `create_unique_constraint("uq_recurring_rules_upsert_key", "recurring_rules", ["merchant_id", "amount_min", "amount_max", "currency", "period_days"])`. Downgrade drops the constraint (table and data preserved).
- `tests/test_recurring.py` — 5 new unit tests for confidence sanitization
- `tests/test_alembic.py` — 3 new alembic tests (constraint creation, dedup, downgrade)
- `openspec/changes/.../verify-report.md` — Status PASS
- `openspec/changes/.../apply-progress.md` — All tasks complete, 5 commit SHAs

## SDD Cycle History
- **Proposal**: written, scope = schema validator + migration 0008 + tests
- **Spec**: 2 new ADDED Requirements (NaN/inf sanitization + unique constraint), 10 new scenarios
- **Tasks**: 4 tasks planned, ~200 LOC estimated, single PR
- **Apply**: 3 commits
- **Verify (1st)**: FAIL — "Downgrade drops the unique constraint" scenario had no dedicated test
- **Apply (correction)**: 1-line test added (`test_alembic_0008_downgrade_drops_unique_constraint_preserves_data`) in commit `b6ff6fd`
- **Verify (2nd)**: PASS — all 10 spec scenarios covered, no regressions

## What Was NOT Delivered
- No detector algorithm changes (`app/services/recurring_detection.py` untouched)
- No new API endpoints
- No new spec files in `openspec/changes/` other than the delta
- The composite read-side index `ix_recurring_rules_merchant_currency_period` is preserved (not replaced)

## Source of Truth
- **New main spec created**: `openspec/specs/phase2-recurring-detection/spec.md` (first promotion of the spec to the canonical location; previously lived in `openspec/changes/phase-2-pr5-recurring-detection/specs/...`)
- The merged spec = 7 existing requirements (preserved) + 2 new ADDED requirements (NaN/inf sanitization + unique constraint)

## Spec Promotion Note
This is the first time the `phase2-recurring-detection` spec is promoted to `openspec/specs/`. The spec previously lived at `openspec/changes/phase-2-pr5-recurring-detection/specs/...` because PR #5's archive step was performed on the user's working branch (`feat/phase2-pr3-categories-ui`), not on `main`. This archive step completes the promotion.

## Task Completion Gate — Stale-Checkbox Reconciliation
At the start of archive, all 11 task checkboxes in `tasks.md` were still `- [ ]` (the `sdd-apply` executor did not persist the completion state into the task artifact — `apply-progress.md` was complete, but `tasks.md` was not updated). The skill's Task Completion Gate normally blocks archive in that state. Per the skill's exception clause, the orchestrator explicitly authorized the reconciliation because:

1. `apply-progress.md` shows all 4 task groups as `[x]` with commit SHAs, pytest output (49/49 focused, 348/348 full), ruff/mypy/alembic round-trip results.
2. `verify-report.md` status = PASS, with cross-walk from every spec requirement to its implementation + tests.
3. The git log confirms 5 implementation commits on `feat/recurring-info-fixes` ahead of `origin/main` @ `46ec7b8`.

`sdd-archive` updated `tasks.md` to mark all checkboxes `[x]` and added the explanation block at the bottom of the file. The audit trail now reflects the true completion state. No code was changed by this reconciliation. This is recorded here so future readers understand the discrepancy between the originally-persisted `tasks.md` and the final archived state.

## Open Items / Followups
- The select-then-insert upsert path in `app/services/recurring_detection.py` is now race-safe at the database level (the unique constraint surfaces concurrent races as `IntegrityError`). The application layer's existing race-guard pattern (per `app/services/merchants.py:419,450,613`) handles this. A future PR could refactor the upsert to use `INSERT ... ON CONFLICT` for cleaner semantics, but this is out of scope.
- The new migration's dedup step is defensive — should be a no-op on production. If a future deployment discovers duplicate rows, the dedup keeps the highest-confidence row per group.
- The pre-existing `phase-2-pr5-recurring-detection` change folder is still on disk in `openspec/changes/` (it was never moved to `archive/` because PR #5's archive step ran on a different branch). That is an inconsistency inherited from PR #5, not introduced by this archive.

## Post-Phase 2 Note
This is the first post-Phase 2 PR. Phase 2 closed with PR #34 (PR #6 — Docs + e2e) at `46ec7b8`. This change addresses the 2 INFO theoretical findings left from PR #5 judgment-day.
