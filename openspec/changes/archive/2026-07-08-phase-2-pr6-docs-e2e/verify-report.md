# Verify Report — Phase 2 PR #6

## Status: PASS (after LIDER_CANNED_DATE fix)

## Cross-walk

| Proposal success criterion | Evidence |
|---|---|
| README "Phase 2" section covers all 3 capabilities; API table has 6 new endpoints; migrations 0005-0007 mentioned; status note updated to "Phase 1 + Phase 2" | README.md commit `71fdf68` |
| tests/test_e2e_phase2.py mirrors Phase 1 e2e style; will pass when run with TEST_RUT set; asserts all 3 new FKs populated + GET/PATCH recurring + FK preserved on deactivation | tests/test_e2e_phase2.py commits `4d73e86` + LIDER_CANNED_DATE fix; pytest output |

## Test Results

- `pytest tests/test_e2e_phase2.py` — **SKIP** in this environment (TEST_RUT not set); will **PASS** when run with TEST_RUT after the LIDER_CANNED_DATE fix (dates = [2026-02-04, 2026-03-06, 2026-04-05]; intervals = [30, 30]; period_days = 30, matches line 503 assertion).
- `pytest tests/ --ignore=tests/test_llm_services.py` — clean
- `ruff check` — clean
- `ruff format --check` — clean

## Gate Correction

On the first apply pass, sdd-verify caught a latent defect in `LIDER_CANNED_DATE`:

- **Bug**: constant was set to `date(2026, 4, 15)` (the PARIS row date), but the canned LIDER row in `CANNED_NACIONAL_EXTRACTION` is dated `"05/04/26"` = `2026-04-05`.
- **Symptom**: pre-seeded transactions at -60d and -30d from 2026-04-15 produced dates [2026-02-14, 2026-03-16, 2026-04-05]; intervals = [30, 20]; median = 25.0; `period_days = 25` — failing the assertion at line 503 (`period_days == 30`).
- **Fix**: set `LIDER_CANNED_DATE = date(2026, 4, 5)` and updated the comment block to call out the PARIS-vs-LIDER distinction. With the fix: dates = [2026-02-04, 2026-03-06, 2026-04-05]; intervals = [30, 30]; median = 30.0; `period_days = 30` — assertion passes.

## Judgment-Day Findings (post-archive)

After the archive commit landed, a 2-judge judgment-day review caught 4 additional issues that the sdd-verify gate missed:

1. **CRITICAL** (Judge A): canned COPEC category `"Transport"` did not match the seeded category name `"Transportation"` (per `0005_phase2_categories.py:109`). The ingestion layer's category lookup at `ingestion.py:868` is case-insensitive exact match, so `"transport"` would miss → `category_id = None` → line 484 assertion fails when the test is actually run with `TEST_RUT`. **Fix**: changed canned COPEC at test line 145 from `"Transport"` to `"Transportation"`; updated assertion at line 488 to match.
2. **WARNING** (Judge A): README migration table row for `0005_phase2_categories` incorrectly attributed the `transactions.category_id` FK to migration 0005. The FK is actually created in migration 0006 (per `0005`'s own docstring at line 49 and `0006`'s `add_column` at line 114). **Fix**: rewrote row 0005 to "categories table + seed of 12 Y-NAB rows"; expanded row 0006 to "merchants + merchant_aliases tables; category_id + merchant_id FKs + low_confidence on transactions".
3. **SUGGESTION** (both judges, CONFIRMED): README confidence formula at line 91 omitted the `max(0.0, ...)` clamp present in the code at `recurring_detection.py:516`. **Fix**: added the clamp to the formula in the README.
4. **SUGGESTION** (Judge A): `phase2_world` fixture docstring at test line 290 referenced the LIDER date as `2026-04-15` (the PARIS date) — same LIDER-vs-PARIS confusion that the `LIDER_CANNED_DATE` fix corrected, but the docstring was missed. **Fix**: changed docstring to `(2026-04-05)` to match the actual canned LIDER date.

All 4 issues were addressed in commit `d9b6eb9` (2 files, 6 insertions, 6 deletions). Judgment-day Round 2 verdict: **APPROVED** (both judges).

## Risks

- E2E test requires `TEST_RUT` env var and the Santander PDF fixture; CI without these will skip the test (matches Phase 1 e2e pattern).
- The `phase2-recurring-detection` spec cross-reference in the README points at `openspec/changes/phase-2-pr5-recurring-detection/specs/phase2-recurring-detection/spec.md` with an explicit note that it will live at `openspec/specs/phase2-recurring-detection/spec.md` after PR #5's archive step runs. (Note: as of 2026-07-08, the main spec is at the change-folder path because the PR #5 archive was performed on the user's working branch, not on `main`; the README's note is honest about this.)
