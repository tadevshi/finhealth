# Tasks: Phase 2 PR #3 — Categories UI

## Summary

~240 LOC. Single PR (`pr-3`, base `main@1ff5316`). ~10 new tests (366→~376). Coverage target ≥83.17%. Cherry-pick gate: `git diff main -- app/services/ingestion.py` MUST be empty. Anchored to engram #90/#91/#92/#93 and decision #12.

## Review Workload Forecast

Estimated changed lines: ~240. 400-line budget risk: Low. Chained PRs recommended: No (single PR). Delivery strategy: force-chained (N/A). Chain strategy: N/A.

Decision needed before apply: No
Chained PRs recommended: No
Chain strategy: N/A
400-line budget risk: Low

## Phase 1 — Foundation (refactor)

- [x] **1.1 `refactor(web): extract _query_transactions helper in router.py`** *(D2)*
  - Pull the duplicated 10-filter chain from `transactions_page` (L124-207) and `transactions_rows_partial` (L222-291) into `_query_transactions(session, *, statement_id, date_from, date_to, min_amount, max_amount, description, currency, category_id, uncategorized, limit, offset) -> list[Transaction]`. Both endpoints call it. Drop the L164-165 TODO.
  - **Files**: `app/web/router.py`. **Acceptance**: helper exists; both endpoints use it; existing web tests pass; behavior unchanged. **Deps**: none.

## Phase 2 — API (Query filters + Accept dispatch)

- [x] **2.1 `feat(api): add category_id and uncategorized filters to list_transactions`**
  - Add 2 Query params to `list_transactions` (L130-216): `category_id: list[uuid.UUID] | None = Query(default=None)` and `uncategorized: bool = Query(default=False)`. Parenthesized `or_`: both → `IN (...) OR IS NULL OR low_confidence=True`; category_id only → `IN (...)`; uncategorized only → `(IS NULL OR low_confidence=True)`; neither → no WHERE. 4 tests cover the 4 combos.
  - **Files**: `app/api/v1/transactions.py`, `tests/test_transactions.py`. **Acceptance**: 4 new tests pass; existing tests pass; SQL parameter order stable. **Deps**: none (columns exist from PR #2).

- [x] **2.2 `feat(api): add Accept header dispatch to PATCH /api/v1/transactions/{id}`** *(D1)*
  - In `update_transaction` (L204-229), read `request.headers.get("accept", "")`. `text/html` → `HTMLResponse(content=rendered_partial, 200)` reusing the partial template; default → `TransactionResponse` (unchanged). Keep `response_model=TransactionResponse` on the decorator. One `Category` SELECT supplies the `selected` option. 2 tests: text/html returns HTML, application/json (or no header) returns JSON.
  - **Files**: `app/api/v1/transactions.py`, `tests/test_transactions.py`. **Acceptance**: text/html returns partial with new `category_id` `selected`; JSON branch preserves contract; both branches do the write-through. **Deps**: 1.1, 3.1.

## Phase 3 — Web UI (select, filter form)

- [x] **3.1 `feat(web): render category <select> in transactions_table partial`**
  - Replace the free-text `<input type="text" name="category">` at `partials/transactions_table.html:47-62` with a server-rendered `<select name="category_id">` whose 13 `<option>`s are the 12 categories (sorted by `sort_order`) + blank "—". Keep `hx-patch`, `hx-trigger`, `hx-target`, `hx-swap`. Set `selected` on the current `category_id`. Add `categories: list[Category]` to `transactions_page`'s context (load via the same `upload_page` `banks` pattern). 2 tests: renders 13 options; current `category_id` is `selected`.
  - **Files**: `app/web/templates/partials/transactions_table.html`, `app/web/router.py`, `tests/test_web_phase1.py` (or new `tests/test_web_phase3.py`). **Acceptance**: `<select>` renders 12 + blank options; PATCH round-trip works; current `category_id` is `selected`. **Deps**: 1.1, 2.1.

- [x] **3.2 `feat(web): add multi-select + uncategorized checkbox to filter form`** *(D3, D4, D5)*
  - Add to `transactions.html:35-167`: a `<select multiple name="category_id">` with 12 `<option>`s (sorted by `sort_order`) and a `<input type="checkbox" name="uncategorized" value="true">` labeled **"Untagged or low confidence"** (D3; disambiguates from seeded `Uncategorized` per D4). The 2 params ride the helper from 1.1. 2 tests: form has both controls; submission narrows the table.
  - **Files**: `app/web/templates/transactions.html`, `app/web/router.py`, `tests/test_web_phase1.py`. **Acceptance**: form has both controls; submission narrows the list; `transactions_rows_partial` accepts the 2 filters. **Deps**: 1.1, 2.1, 3.1.

## Phase 4 — Test consolidation + SDD artifacts

- [x] **4.1 `test(api,web): consolidate coverage for category filters, partial, and PATCH dispatch`**
  - Ensure all new tests from 2.1, 2.2, 3.1, 3.2 are present, named cleanly, and run. Add edge cases: `Untagged` checkbox when both filters are set; `None` `category_id` PATCH round-trip; multi-select serialization of 3+ UUIDs.
  - **Files**: `tests/test_transactions.py`, `tests/test_web_phase1.py` (or new test file). **Acceptance**: suite green; new paths covered; coverage ≥83.17%. **Deps**: 2.1, 2.2, 3.1, 3.2.

- [x] **4.2 `chore(sdd): add SDD artifacts for phase-2-pr3-categories-ui`**
  - Write `apply-progress.md` incrementally as tasks land. On the final commit record: tasks done, commit SHAs, test count delta, coverage delta, cherry-pick isolation gate result, PR URL. Mark this `tasks.md` all `[x]` once apply finishes.
  - **Files**: `openspec/changes/phase-2-pr3-categories-ui/apply-progress.md`, this `tasks.md`. **Acceptance**: `apply-progress.md` is in the change folder when the PR opens; all checkboxes `[x]`. **Deps**: none (parallel with 1.1).

## Cherry-pick isolation gate (CRITICAL)

`git diff main -- app/services/ingestion.py` MUST be empty. The only production changes are: `app/api/v1/transactions.py` (2 new Query params + PATCH dispatch), `app/web/router.py` (2 new Query params + `categories` context + `_query_transactions` helper), `app/web/templates/partials/transactions_table.html` (`<input>` → `<select>`), `app/web/templates/transactions.html` (multi-select + checkbox).

## Hard constraints

NO new migration / column / model / endpoint. DO NOT modify `Category` model, the `categories` migration, the PATCH request handling (only the response shape per D1), or any production code outside the 4 files above. NO `Co-Authored-By` or AI attribution. NO skipping tests, lint, or type gates. NO new files outside the target list (except `apply-progress.md` and any new test file if needed).
