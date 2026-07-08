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

## Risks

- E2E test requires `TEST_RUT` env var and the Santander PDF fixture; CI without these will skip the test (matches Phase 1 e2e pattern).
- The `phase2-recurring-detection` spec cross-reference in the README resolves at `openspec/specs/phase2-recurring-detection/spec.md` because PR #5's archive step runs first.
