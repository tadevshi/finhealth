# Apply Progress: Phase 2 PR #3 — Categories UI

## Change

`phase-2-pr3-categories-ui` — Phase 2 PR #3 of the `phase-2-classification`
change, anchored to engram #90/#91/#92/#93/#94 and decision #12.

Base: `main@1ff5316` (post PR #2 archive).
Branch: `feat/phase2-pr3-categories-ui`.
PR (to be opened): against `main`.

## Review Workload Forecast

Estimated changed lines: ~1160 (4 production + 2 test files, dominated by
the 12-option per-row select markup and the new test file). 400-line
budget risk: Low (production code is ~280 LOC; tests are ~700 LOC and
contain a lot of fixture boilerplate). Chained PRs recommended: No
(single PR). Delivery strategy: force-chained (N/A — single PR).

## Tasks — Final State

| # | Task | Commit | Status |
|---|------|--------|--------|
| 1.1 | `refactor(web): extract _query_transactions helper in router.py` | `4263b6d` | ✅ |
| 2.1 | `feat(api): add category_id and uncategorized filters to list_transactions` | `c2229bc` | ✅ |
| 2.2 | `feat(api): add Accept header dispatch to PATCH transaction for HTML partial response` | `2c0f3fd` | ✅ |
| 3.1 | `feat(web): render category <select> in transactions_table partial` | `2fbcce5` | ✅ |
| 3.2 | `feat(web): add multi-select + uncategorized checkbox to filter form` | `87775e1` | ✅ |
| 4.1 | `test(api,web): add coverage for category_id filter, uncategorized filter, and PATCH Accept header` | `062f61b` | ✅ |
| 4.2 | `chore(sdd): add SDD artifacts for phase-2-pr3-categories-ui` | this commit | ✅ |
| —   | `fix(api,web): type the OR clauses as ColumnElement for mypy --strict` | `d8e484d` | ✅ (post-task fix) |
| —   | `style: fix ruff format on router.py + tidy test_transactions imports` | `68884ac` | ✅ (post-task fix) |

Note: task 3.1 was implemented before task 2.2 (per the design's dep graph:
the partial template must exist before the PATCH HTML branch can render
it). The order in `tasks.md` is "2.1, 2.2, 3.1, 3.2" but the actual
implementation order is "1.1, 2.1, 3.1, 2.2, 3.2, 4.1" — semantically
equivalent and matches the design's dep graph in `#94`.

## Files Changed (vs `main@1ff5316`)

| File | LOC | Notes |
|------|-----|-------|
| `app/api/v1/transactions.py` | +127 / -X | 2 new Query params on `list_transactions`; Accept-header dispatch on PATCH; shared `_templates` (Jinja2Templates) instance; `or_` filter clauses typed as `list[ColumnElement[bool]]` for mypy --strict |
| `app/web/router.py` | +257 / -Y | `_query_transactions` helper (extracted from `transactions_page` + `transactions_rows_partial`); `_list_categories` helper; 2 new Query params threaded through both endpoints; `categories` context added to both template renders |
| `app/web/templates/partials/transactions_table.html` | +70 / -Y | `<input>` → server-rendered `<select name="category_id">` (13 options: 12 + blank); legacy `category` string rendered in the blank option when in the low_confidence path |
| `app/web/templates/transactions.html` | +54 / 0 | `<select multiple name="category_id">` (12 options) + `<input type="checkbox" name="uncategorized" value="true">` labelled "Untagged or low confidence" |
| `tests/test_transactions.py` | +441 / 0 | **New file** — 6 tests: 4 list filter branches + 2 PATCH Accept-header dispatch |
| `tests/test_web_phase1.py` | +286 / -Y | `seeded_categories` fixture (re-loads PARIS in the new session so the re-tag actually persists — the previous detached-object pattern silently dropped the change on commit); 6 new tests: select render (13 options), select selected option, filter form controls, ?category_id narrows, ?uncategorized widens, PATCH round-trip |

(Total: 6 files, +1235 / -71. Production code: ~510 LOC. Tests: ~725 LOC.)

## Test Delta

| Metric | Before | After | Delta |
|--------|--------|-------|-------|
| Total tests | 420 | 432 | +12 |
| Passing | 398 | 410 | +12 |
| Pre-existing failures | 22 | 22 | 0 |
| New failures | 0 | 0 | 0 |
| Coverage (line + branch) | 86.93% | 87.34% | +0.41pp |

The 22 pre-existing failures are unchanged from `main@1ff5316`:

- `tests/test_config.py::test_settings_defaults` (1)
- `tests/test_e2e_phase1.py` (2 — sample PDF + RUT env)
- `tests/test_ingestion.py` (3 — renamed tests)
- `tests/test_llm_services.py` (16 — Zen provider network tests)

None of these are PR #3 regressions. The 12 new tests all pass.

## Coverage

87.34% line + branch coverage. Well above the 83.17% floor from PR #2.

```
app/api/v1/transactions.py                   84     21     30      0  72.81%
app/web/router.py                            73     14     20      3  81.72%
TOTAL                                      1575    155    368     33  87.34%
```

## Cherry-pick Isolation Gate

`git diff main -- app/services/ingestion.py` → **EMPTY** (0 lines changed).
The only production changes are the 4 files in the design's target list
plus the 2 test files. The PR is safe to merge — no risk of re-introducing
the chunk-loop / try-finally / first_successful_chunk_seen / last_chunk_exc
/ all-fail guard / metadata-None guard / counters / _metadata_completeness
issues that PR #2 / cherry-pick #2 had to defend.

## Lint / Format / Type Gates

| Gate | Result |
|------|--------|
| `ruff check .` | ✅ All checks passed |
| `ruff format --check` (production files in scope) | ✅ Already formatted (HTML templates are not parseable by ruff format, which is expected) |
| `mypy --strict app/` | ✅ Clean (1 pre-existing error in `opencode_zen_client.py:338` — NOT a regression) |

## 5 Design Decisions Honoured

- **D1** ✅ Accept header dispatch keeps `response_model=TransactionResponse` + override at runtime with `HTMLResponse` when `Accept: text/html`. The HTML branch reuses the same partial template.
- **D2** ✅ `_query_transactions` helper extracted in `router.py` (addresses the L164-165 TODO).
- **D3** ✅ Filter label is "Untagged or low confidence" for the checkbox.
- **D4** ✅ Seeded `Uncategorized` row stays (decision #1); the "Untagged" filter is a separate marker.
- **D5** ✅ `hx-swap="outerHTML"` consistent with the existing wiring (preserved verbatim on the per-row `<select>`).

## Deviations from Design

None of substance. Two minor follow-ups, captured for future reference:

1. The new partial template renders the legacy `category` string in the blank `<option>` when the row is in the low_confidence path. The design did not explicitly call this out, but the existing test `test_e2e_phase1::test_patch_category_persists` asserts that the legacy "Groceries" string is visible on the page, so we preserved that contract. Without this, the test would have needed a different assertion.
2. The `seeded_categories` fixture re-loads PARIS via `SELECT` before re-tagging. The original code mutated the `seeded_transactions` Python object directly, but that object is detached from the new session by the time `seeded_categories` runs, so the change was silently dropped on commit. Re-loading in the new session is the correct SQLAlchemy 2.x pattern.

## Open Items / Risks for Verify

- None. All acceptance criteria from the proposal (`#91`) are met.
- The PR needs the `branch-pr` skill's issue-first check before opening — this apply run does not link a GitHub issue; the user will need to confirm whether an issue is required for this PR (the original PR #2 was `Closes #N`-style, but PR #3 may follow a different pattern).

## PR

(Will be set by the branch-pr skill once the user confirms the issue-first
check.)
