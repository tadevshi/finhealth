# Archive Report — phase-2-pr5-recurring-detection

## Status: MERGED + ARCHIVED (delayed archive)

## Change Summary
- **Change name**: `phase-2-pr5-recurring-detection`
- **PR**: #33 (https://github.com/tadevshi/finhealth/pull/33)
- **Branch**: `feat/phase2-pr5-recurring-detection` (merged + deleted 2026-07-08)
- **Base**: `origin/main` @ `1ff5316` (Phase 2 PR #2 merged)
- **Merge commit**: `647229c159db9d827b66dd1ce8a6bc2db2c2b372` on `origin/main`
- **Merge date**: 2026-07-08
- **Final commits on branch**: 18 (8 implementation + 10 judgment-day fix commits)
- **Tests at merge**: 25 passing in `tests/test_recurring.py` (was 18 before JD); 340 total, 0 regressions

## What Was Delivered
- `app/services/recurring_detection.py` (597 lines) — `RecurringDetector` service: 90-day scan, ±15% tolerance, median cadence classification, upsert by composite key, confidence score, FK backfill
- `app/models/recurring_rule.py` (175 lines) — `RecurringRule` model with `noload` relationships
- `app/api/v1/recurring.py` — `GET /api/v1/recurring` + `PATCH /api/v1/recurring/{id}` endpoints
- `app/schemas/domain.py` — `RecurringRuleResponse` + `RecurringRuleUpdate` Pydantic models
- `alembic/versions/0007_phase2_recurring_rules.py` — new migration: `recurring_rules` table + 2 indexes + `transactions.recurring_rule_id` FK
- `app/services/ingestion.py` (+6 LOC additive) — `RecurringDetector` wired into `ingest_statement` with `try/except` wrapper
- `tests/test_recurring.py` (~1457 lines at merge) — 25 tests (12 algorithm + 4 API + 9 edge cases / spec coverage)
- `README.md` (partial — full Phase 2 section added in PR #6)

## Judgment-Day History (this PR's audit trail)
The PR went through **3 rounds of judgment-day** with 2 blind judges each round. The full fix sequence is recorded in the commit history on the merged branch:

**Round 1** (10 findings: 1 CONFIRMED + 9 SUSPECT)
- 1 CONFIRMED CRITICAL: duplicate `_classify_period` call (line 360) — fixed in commit `eeab82b`
- 9 SUSPECT findings: spec outlier scenario contradiction, 6 missing spec-coverage tests, same-day guard incomplete, eager loads on relationships, missing verify-report, comment-code mismatch, 90-day boundary test (3 fixtures vs spec 4)
- 6 fix commits: `eeab82b` (duplicate), `d0d5f5a` (same-day initial), `3749c8a` (noload), `57c9a6b` (spec reconciliation), `72c4388` (6 tests + 4th boundary row), `61d7174` (verify-report)

**Round 2** (3 findings: 1 CONFIRMED + 2 SUSPECT)
- 1 CONFIRMED WARNING: spec/code median disagreement (spec said 0.5707 with in-band median, code computes 0.5711 with full-group median; test tolerance 0.001 masked the gap)
- 2 SUSPECT: same-day guard incomplete (3 same-day + 1 later → intervals [0,0,30] → median 0 still produced meaningless weekly/0), stale `selectin` docstring after noload change
- 2 fix commits: `f8b3bb1` (tightened same-day guard), `706dd5e` (spec/test median reconciliation to full-group median 10.375 / 0.5711 with 0.0001 tolerance)

**Round 3** (2 findings: 2 SUSPECT, Judge A only)
- SUSPECT WARNING: `session.refresh(rule, attribute_names=[...])` opt-in recipe doesn't work for `lazy="noload"` on SQLAlchemy 2.0.51 — empirically verified; working opt-in is `selectinload` at query time
- SUSPECT WARNING: same-day fix's regression test only covered the all-zero intervals case (which the old guard also caught), not the [0,0,30] case the fix targets
- 2 fix commits: `afcd8ec` (corrected noload opt-in docstring), `4594073` (majority-same-day regression test + verify-report cleanup)

**Final verdict**: APPROVED (both judges in Round 2 + Round 3, no remaining real issues)

## What Was NOT Delivered
- No new recurring detection algorithm improvements (algorithm is final)
- No recurring UI (deferred per decision #12 — PATCH is the per-item override path)
- No LLM-driven recurring rules (algorithm is deterministic + statistical)
- `deactivated_at` column on `RecurringRule` (out of scope)
- Sentry alerts for detector failures (decision #14: log only)
- `credit_card_id` in the `RecurringRule` model (would require schema migration; deferred as future work)

## Spec Status
The spec at `openspec/changes/phase-2-pr5-recurring-detection/specs/phase2-recurring-detection/spec.md` is the **original 7-requirement delta** from this PR (preserved here for historical reference).

The **canonical spec** for `phase2-recurring-detection` lives at `openspec/specs/phase2-recurring-detection/spec.md` and contains 9 requirements: the 7 original + 2 new requirements added by PR #7 (recurring-info-fixes, PR #35):
- **Requirement 8**: `RecurringRuleResponse.confidence` sanitizes non-finite values (NaN, +inf, -inf) to `0.0` via `field_validator(mode="before")` — defensive
- **Requirement 9**: `recurring_rules` has a `UNIQUE` constraint `uq_recurring_rules_upsert_key` on the 5-tuple `(merchant_id, amount_min, amount_max, currency, period_days)` — DB-level race safety

## Archive Delay Note
This archive step was **delayed by one full session cycle**. The original sdd-archive step happened inside the PR #33 worktree (a worktree that was later removed after merge). When the worktree was cleaned up, the `mv openspec/changes/phase-2-pr5-recurring-detection/ → archive/` did not persist to `main`. This archive step is completing the originally-planned move.

**Consequence**: there is no `openspec/changes/archive/2026-07-08-phase-2-pr5-recurring-detection/archive-report.md` written at the time of the original merge. This report is the missing audit record. The merge commit, commit SHAs, and the 3-round judgment-day history are all preserved in the git history of the deleted `feat/phase2-pr5-recurring-detection` branch (which was merged into `main` at `647229c` before deletion).

## Next Steps (closed by subsequent work)
- PR #6 (phase-2-pr6-docs-e2e) — README Phase 2 section + e2e test (merged at `46ec7b8`, 2026-07-08). Closes the 6th and final PR of the Phase 2 plan.
- PR #7 (recurring-info-fixes) — closed 2 INFO theoretical findings (NaN/inf defense + unique constraint) + promoted the spec to `openspec/specs/` (merged at `2ccc153`, 2026-07-08).
- Phase 2 is now fully complete: 5 PRs in the plan + 1 residual fix PR all merged.
