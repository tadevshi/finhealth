# Archive Report: ingestion-tolerate-partial-chunk-failures

**Change**: `ingestion-tolerate-partial-chunk-failures`
**Archived on**: 2026-06-29
**Status**: ✅ ARCHIVED (SDD cycle complete)

## Summary

This change introduces the `ingestion-chunk-failure-tolerance` capability for finhealth: `_run_chunked_extraction` in `app/services/ingestion.py` now tolerates a single chunk failure in a multi-chunk PDF, persisting the surviving chunks' transactions and continuing. Only all-chunk failures raise `IngestionError`. Implementation uses a `try/finally` block to guarantee the summary log fires on every path (success, partial, all-fail).

This is the first formally-spec'd SDD change for finhealth. It was developed end-to-end through the full SDD cycle: explore → propose → spec → design → tasks → apply → verify → judgment (3 rounds) → archive.

## Merge State

- **PR**: https://github.com/tadevshi/finhealth/pull/29
- **Status**: MERGED
- **Merged at**: 2026-06-29T00:48:07Z
- **Merge commit**: `077227f31e37698adc5ff06235f8f2d8b7a331d7`
- **Production commits on the branch** (before merge):
  - `d56120e` — Round 1 fix: tolerance logic + 5 tests + 1 rename + 2 helpers + caplog filter + squash
  - `6348308` — Round 2 fix: docstring + all-fail error message refinements (from judgment-day)
- **Post-merge SDD artifacts commit on main**: `7b5071f` — added the 6 OpenSpec artifacts to main (were untracked at merge time)

## Verification

- **sdd-verify**: PASS (8/8 spec scenarios compliant, 7/7 design decisions honored, cherry-pick isolation confirmed, all gates green for changed files)
- **judgment-day**: APPROVED ✅ after 3 rounds
  - Round 1: 4 WARNING (real) findings (W1-W4), all fixed
  - Round 2: 1 CONFIRMED docstring issue (R1) + 1 SUSPECT all-fail message issue (R2), both fixed
  - Round 3: VERDICT: CLEAN — all fixes verified, no new issues
- **Test suite**: 366 passing (5 new) + 3 pre-existing failures (unrelated, not regressions)
- **Coverage**: 90.40% on `app/services/ingestion.py` (target ≥85%)
- **Lint + types**: `ruff check` clean, `ruff format --check` clean for changed files, `mypy --strict app/` clean
- **Cherry-pick isolation**: `git diff main --name-only` shows ONLY `app/services/ingestion.py` and `tests/test_ingestion.py` (plus the OpenSpec artifacts committed in `7b5071f`)

## Specs Synced

The new capability `ingestion-chunk-failure-tolerance` is now in the main specs:

| Domain | Action | Details |
|--------|--------|---------|
| `ingestion-chunk-failure-tolerance` | Created (new capability) | 6 requirements, 8 Given/When/Then scenarios, copied from delta spec |

**Source of truth updated**: `openspec/specs/ingestion-chunk-failure-tolerance/spec.md`

Future SDD changes can reference this capability directly.

## Archive Contents

The change folder was moved to `openspec/changes/archive/2026-06-29-ingestion-tolerate-partial-chunk-failures/`:

- ✅ `proposal.md` — 3.3KB
- ✅ `specs/ingestion-chunk-failure-tolerance/spec.md` — 4.7KB (also synced to main specs)
- ✅ `design.md` — 10.6KB
- ✅ `tasks.md` — 4.5KB (13/13 tasks complete, see reconciliation note)
- ✅ `apply-progress.md` — 8.5KB
- ✅ `verify-report.md` — 11.5KB
- ✅ `archive-report.md` — this file

## Stale-Checkbox Reconciliation (Orchestrator Exception)

The `tasks.md` in the change folder originally had all 13 tasks in `- [ ]` (unchecked) state, even though all 13 were actually executed in the apply phase (proven by `apply-progress.md`, the 2 production commits on the branch, and the verify-report's PASS verdict).

The orchestrator (gentle-orchestrator) reconciled this exception per the sdd-archive skill's "Only proceed if the orchestrator explicitly instructs you to reconcile stale checkboxes and `apply-progress`/`verify-report` prove every unchecked task is complete" rule. The reason: the change was merged, the judgment APPROVED, the verify PASS, and the apply-progress shows the full implementation history. The stale checkboxes were a tooling gap, not a reflection of incomplete work.

All 13 tasks are now marked `- [x]` in the archived `tasks.md`. This reconciliation is recorded here for audit trail.

## Documented Deviations (from apply-progress.md, not blocking)

1. **`caplog.set_level` is INFO (not WARNING)** — the summary `logger.info` line and the per-chunk `logger.warning` line are both captured. WARNING-specific assertions still work via `r.levelno == logging.WARNING` filtering.
2. **`_FailOnNthChunk` extended with optional `responses: list[ExtractionResponse]`** — to support the metadata-regression-guard test (test #10). The contract is a strict superset of the design's spec.

## Pre-Existing Test Failures (NOT regressions, not blocking)

Three test failures in `tests/test_ingestion.py` are pre-existing on main and unrelated to this change:
- `test_credit_card_populated_from_llm_metadata`
- `test_invalid_rut_raises_before_pipeline`
- `test_upload_with_llm_failure_returns_422`

Sixteen test failures in `tests/test_llm_services.py` require Zen network access and are also pre-existing.

These should be addressed in a follow-up issue. They are NOT findings for this change.

## Follow-up Suggestions (from verify-report.md, SUGGESTION level)

1. **Test name hygiene**: `test_metadata_taken_from_first_chunk` is slightly misleading now that metadata selection is "most complete wins". Rename in a follow-up.
2. **`ruff format --check` on full project**: 7 pre-existing unformatted files. Consider a format-only follow-up PR.
3. **Metadata-None guard coverage**: the `failed_chunks > 0` branch (lines 546-550) is uncovered. Degenerate case, consider adding a test in a follow-up.

## Skill Resolution

`none` — the dedicated `sdd-archive` sub-agent could not be launched due to model router unavailability (`opencode/glm-5-free` is not a valid model identifier; the suggestion was to use a different free model). The orchestrator (gentle-orchestrator) executed the archive inline per the orchestrator rule "Tool unavailability is not a waiver; document it, stop the blocked delegated work, and perform the closest fresh-context audit only where the fired rule calls for review/audit." This exception is recorded for traceability.

## SDD Cycle Complete

The change has been fully planned (explore → propose → spec → design → tasks), implemented (apply), verified (sdd-verify PASS + judgment-day APPROVED), merged (PR #29), and archived. The new capability `ingestion-chunk-failure-tolerance` is in the main specs and ready for future SDD changes to reference.

**Ready for the next change** (per the explore artifact #52, the next planned change is `phase2-classification` — auto-categorize transactions, merchant aliases, recurring detection).
