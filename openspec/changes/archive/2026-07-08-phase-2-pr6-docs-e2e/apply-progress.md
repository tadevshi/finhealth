# Apply Progress — Phase 2 PR #6

## Summary

3 tasks completed in 3 commits. No code in `app/`, `alembic/`, or
`pyproject.toml` touched — PR is docs + test only.

## Tasks

### Task 1: `docs: update README with Phase 2 section, endpoints, migrations, status`

- **Commit:** `71fdf68`
- **Files changed:** `README.md` (+88 / -6)
- **What:** Updated top-of-file "Status" to "Phase 1 + Phase 2 are live";
  inserted "Available now (Phase 2 — Classification, Merchant Resolution &
  Recurring Detection)" section after Phase 1 with one paragraph per
  capability (categories, merchant-aliasing, recurring-detection) and
  cross-references to the 3 spec files; added 6 new endpoints to the
  v1 API table (GET/POST categories, GET/POST merchants/aliases,
  GET/PATCH recurring) with the actual HTTP methods verified in
  `app/api/v1/*.py`; added a migrations table mentioning 0005-0007 by
  filename and phase.
- **Verified:** every claim cross-checked against
  `app/services/recurring_detection.py` (90-day window, ±15% tolerance,
  ≥3 threshold, confidence formula), `app/services/merchants.py`
  (KNOWN_MERCHANT_PATTERNS, S.A./S.A.C./SpA suffix strip), and
  `app/api/v1/{categories,merchants,recurring}.py` (endpoint methods).

### Task 2: `test(e2e): add Phase 2 happy-path test on Santander fixture`

- **Commit:** `4d73e86`
- **Files changed:** `tests/test_e2e_phase2.py` (+561)
- **What:** New e2e test mirroring `tests/test_e2e_phase1.py` (same
  `FakeLLMClient`, same `CANNED_NACIONAL_EXTRACTION`, same
  `SANTANDER_PDF`, same `needs_sample_pdf` + `needs_test_rut` skip
  markers). Pre-seeds 1 LIDER merchant + 1 alias
  (``alias_text="SUPERMERCADOS LIDER"``, ``source='auto'``) + 2
  historical LIDER transactions on the same `credit_card_id` dated
  60d and 30d back from the canned LIDER row (2026-04-15). The
  canned 3rd LIDER row from the LLM trips the detector's 3-occurrence
  threshold and produces a monthly rule. Assertions cover all 4
  Phase 2 capabilities: 3 FKs populated (`merchant_id`,
  `category_id`, `recurring_rule_id` on the LIDER row),
  `GET /api/v1/recurring` returns the LIDER rule with
  `period_label="monthly"`, `POST /api/v1/categories/{id}` rename
  returns 200, `GET /api/v1/merchants` returns the LIDER merchant,
  `PATCH /api/v1/recurring/{id}` `is_active=false` returns 200 and
  excludes the rule from the next `GET`, and the FK on the LIDER
  transaction is preserved on deactivation (design D).
- **Verified:** `ruff check` and `ruff format --check` clean;
  `pytest tests/test_e2e_phase2.py --no-header -q` skips cleanly
  without `TEST_RUT` and without the sample PDF (`1 skipped`),
  matching the Phase 1 e2e pattern.

### Task 3: `chore(sdd): write verify-report and apply-progress for PR #6`

- **Commit:** _this commit_ (the commit that introduces this file)
- **Files changed:** `openspec/changes/phase-2-pr6-docs-e2e/verify-report.md` (new),
  `openspec/changes/phase-2-pr6-docs-e2e/apply-progress.md` (new)
- **What:** Cross-walk SC1/SC2/SC3 to commit SHAs; record pytest
  output, ruff exits, and the 2 known risks (TEST_RUT dependency,
  PR #5 archive ordering).

## Diff Stats

```
 README.md                                          |  94 +++-
 .../changes/phase-2-pr6-docs-e2e/apply-progress.md |  89 +++
 .../changes/phase-2-pr6-docs-e2e/verify-report.md  |  21 +
 tests/test_e2e_phase2.py                           | 561 +++++++++++++++++++++
 4 files changed, 759 insertions(+), 6 deletions(-)
```

Under the 800-line review budget.

## Ruff Exits

- `ruff check tests/test_e2e_phase2.py` — exit 0
- `ruff format --check tests/test_e2e_phase2.py` — exit 0

## Pytest Result (Local)

```
$ python -m pytest tests/test_e2e_phase2.py --no-header -q
s                                                                        [100%]
1 skipped in 0.01s
```

Skips cleanly without `TEST_RUT` (the `needs_test_rut` marker is the
first guard to fire; the `needs_sample_pdf` marker would also skip
in environments without the gitignored sample PDF).

## Gate-Correction Pass (sdd-verify → sdd-apply re-run)

After the first apply pass, `sdd-verify` caught a latent data bug
in the test that would fail the assertion at line 503 when run with
`TEST_RUT`. Re-ran `sdd-apply` with the corrective fix.

### Task 4: `fix(test): correct LIDER_CANNED_DATE in test_e2e_phase2`

- **Commit:** `71e0955`
- **Files changed:** `tests/test_e2e_phase2.py` (+9 / -8),
  `openspec/changes/phase-2-pr6-docs-e2e/verify-report.md` (+12 / -3)
- **What:** Changed `LIDER_CANNED_DATE = date(2026, 4, 15)` →
  `date(2026, 4, 5)` and rewrote the comment block to call out the
  PARIS-vs-LIDER distinction. The canned LIDER row in
  `CANNED_NACIONAL_EXTRACTION` is dated `"05/04/26"` = 2026-04-05;
  the previous value (2026-04-15) was the PARIS row date.
  Updated `verify-report.md` to reflect the corrected state
  (status now notes the gate correction; math walk-through
  included).
- **Math:** with the fix, pre-seeded + canned LIDER dates = 
  [2026-02-04, 2026-03-06, 2026-04-05]; intervals = [30, 30];
  median = 30.0; `period_days = 30` — line 503 assertion passes.
- **Verified:** `ruff check` and `ruff format --check` clean;
  `pytest tests/test_e2e_phase2.py` skips cleanly (1 skipped,
  no `TEST_RUT`); `pytest tests/ --ignore=tests/test_llm_services.py`
  = 340 passed, 74 skipped.

## Updated Diff Stats (after gate correction)

```
 README.md                                          |  94 +++-
 .../changes/phase-2-pr6-docs-e2e/apply-progress.md | 113 +++++
 .../changes/phase-2-pr6-docs-e2e/verify-report.md  |  30 +++
 tests/test_e2e_phase2.py                           | 562 +++++++++++++++++++++
 4 files changed, 793 insertions(+), 6 deletions(-)
```

Still under the 800-line review budget.
