# Design: Phase 2 PR #3 — Categories UI

## Context

PR #2 (Categories Foundation) delivered the closed-set 12-category Y-NAB taxonomy, the validation in the ingestion layer, the write-through PATCH endpoint, the stateless rename endpoint, and the GET listing endpoint. The cherry-pick (`ingestion-tolerate-partial-chunk-failures`) is also in main.

PR #3 is the consumer-side surface: replace the free-text category `<input>` with a server-rendered `<select>`, add a "Filter by category" multi-select widget, thread 2 new Query params through `list_transactions`, and add Accept header negotiation to the PATCH endpoint. The change is purely UI + one Query filter — no migration, no new model, no new column.

## Goals & Non-Goals

**Goals:**
- Per-row `<select>` server-rendered from `GET /api/v1/categories`
- "Filter by category" multi-select widget in the filter form
- `category_id` + `uncategorized` Query filters on `list_transactions` (and the web layer)
- PATCH Accept header negotiation (`text/html` → partial, default → JSON)
- PATCH accepts form-encoded body (HTMX browser flow)
- `_query_transactions` helper extraction in `router.py` (eliminates the 10-filter duplication)

**Non-Goals:**
- Bulk category assignment UI (decision #12 says anti-feature)
- The `_metadata_completeness` cherry-pick (already in main from PR #28, untouched by PR #3)
- Phase 2 PRs #4-#6 (Merchants + aliases, Recurring detection, Docs + e2e)
- Renaming the seeded `Uncategorized` category (the seed has it at sort_order=11 with a real UUID; the "Untagged" filter is a DIFFERENT special case)

## Approach

### Code Structure

#### `app/api/v1/transactions.py`

**`list_transactions` (lines 130-216)** — add 2 new Query params:
- `category_id: list[uuid.UUID] | None = Query(default=None)`
- `uncategorized: bool = Query(default=False)`

Build the parenthesized `or_` SQL clause using SQLAlchemy `or_()`. The clause depends on which params are set:
- Neither set → no `WHERE` clause
- `category_id` only → `Transaction.category_id.in_(category_id)`
- `uncategorized` only → `or_(Transaction.category_id.is_(None), Transaction.low_confidence.is_(True))`
- Both set → `or_(Transaction.category_id.in_(category_id), Transaction.category_id.is_(None), Transaction.low_confidence.is_(True))`

**`update_transaction` (PATCH endpoint)** — Accept header negotiation:
- Inspect `request.headers.get("accept", "")`
- If `"text/html" in accept`: return `HTMLResponse(content=rendered_partial, status_code=200)` where `rendered_partial` is the partial row HTML (the same partial used by `transactions_rows_partial`), with the new `category_id` rendered as `selected` in the `<select>`. Wrap in try/except for defensive error handling.
- Otherwise: return the `TransactionResponse` (JSON, unchanged from PR #2).
- PATCH also accepts form-encoded body: change `payload: TransactionCategoryUpdate` to `payload: Annotated[TransactionCategoryUpdate, Form()]`. Add a `_ClearCategoryIdSentinel` field validator to coerce empty-string `category_id` to a clear-intent sentinel (the per-row `<select>`'s "—" option).

Keep `response_model=TransactionResponse` on the decorator (preserves the OpenAPI schema for the JSON branch).

#### `app/web/router.py`

**Extract `_query_transactions` helper** (decision D2):
- Move the duplicated 10-filter chain from `transactions_page` (lines 124-207) and `transactions_rows_partial` (lines 222-291) into a private helper.
- Signature: `_query_transactions(*, session: AsyncSession, filters: TransactionFilters, limit: int = 100, offset: int = 0) -> list[Transaction]`. Keyword-only (`*`) to prevent positional mixups.
- Both endpoints call the helper.

**`transactions_page` and `transactions_rows_partial`** — thread the 2 new Query params:
- `category_id: list[uuid.UUID] | None = Query(default=None)`
- `uncategorized: bool = Query(default=False)`
- Load the `categories: list[Category]` from the database (one query at the start of `transactions_page`, passed in the context). `transactions_rows_partial` doesn't need the categories list (the partial is rendered by `transactions_page`'s response).

#### `app/web/templates/partials/transactions_table.html`

- Replace `<input type="text" name="category">` with `<select name="category_id">`
- 13 `<option>`s: 12 categories (sorted by `sort_order`) + a blank "—" for "no category"
- The `selected` attribute is set on the current `category_id` (or the blank option if `category_id` is null or not in the seeded list)
- The `hx-patch="/api/v1/transactions/{id}"` wiring is unchanged

#### `app/web/templates/transactions.html`

- Add `<select multiple name="category_id">` for 12 UUIDs (sorted by `sort_order`)
- Add `<input type="checkbox" name="uncategorized" value="true">` labeled "Untagged or low confidence"
- Remove the redundant `|default([], true)` filter from `filters.category_id`

### SQL Pattern (parenthesized `or_`)

```python
from sqlalchemy import or_

if category_id and uncategorized:
    stmt = stmt.where(or_(
        Transaction.category_id.in_(category_id),
        Transaction.category_id.is_(None),
        Transaction.low_confidence.is_(True),
    ))
elif category_id:
    stmt = stmt.where(Transaction.category_id.in_(category_id))
elif uncategorized:
    stmt = stmt.where(or_(
        Transaction.category_id.is_(None),
        Transaction.low_confidence.is_(True),
    ))
```

## Decisions

### Decision 1: Accept header dispatch preserves JSON contract

The PATCH handler keeps `response_model=TransactionResponse` on the decorator. The handler inspects `request.headers.get("accept", "")` and returns `HTMLResponse` for `text/html` or `TransactionResponse` (JSON) otherwise. The OpenAPI schema is generated for the JSON branch only; the HTML branch is a raw `HTMLResponse` (the openapi schema just doesn't describe the HTML response, which is fine for a browser-only path).

### Decision 2: Extract `_query_transactions` helper

The current `transactions_page` and `transactions_rows_partial` duplicate the same 10-filter chain. Adding 2 filters means touching BOTH. The TODO at `router.py:164-165` says "If the filter set grows, a small `_query_transactions` helper belongs in this module." PR #3 grows the filter set; the helper extraction is in-scope. ~10 LOC extra, but worth it for testability and DRY.

### Decision 3: Form label "Untagged or low confidence"

The seeded `categories` table has a `Uncategorized` row at sort_order=11 with a real UUID. The "Untagged" filter is DIFFERENT: `category_id IS NULL OR low_confidence=True`. Same name, different semantics. The checkbox label "Untagged or low confidence" is the only way to avoid user confusion.

### Decision 4: Seeded `Uncategorized` row stays

The seeded `Uncategorized` row is a real product decision (decision #1: 12 Y-NAB including `Uncategorized`). The label disambiguation in D3 is enough to keep the user from confusing the two. The "Untagged" filter is a separate marker; the seeded row is what the LLM emits when it can't tag.

### Decision 5: `hx-swap="outerHTML"` consistent with existing wiring

The per-row `<select>` triggers `change`, the swap is the entire row. `hx-swap="outerHTML"` is consistent with the existing PATCH wiring. The new partial includes the `<select>` with the new `selected` option, so the swap updates the UI correctly.

## File-by-file changes

| File | Action | Description | ~LOC |
|------|--------|-------------|------|
| `app/api/v1/transactions.py` | Modify | 2 new Query params on `list_transactions`; Accept header dispatch on PATCH; form-encoded body support; try/except around HTML render | ~80 |
| `app/web/router.py` | Modify | 2 new Query params on page + partial; categories context; `_query_transactions` helper extraction | ~70 |
| `app/web/templates/partials/transactions_table.html` | Modify | `<input>` → `<select>`; defensive blank-selected for stale `category_id` | ~15 |
| `app/web/templates/transactions.html` | Modify | Multi-select + checkbox; redundant `\|default` removed | ~10 |
| `tests/test_transactions.py` | New | 4 filter-branch tests + 1 empty-string PATCH test | ~50 |
| `tests/test_web_phase1.py` | Modify | 6 web UI tests + 1 multi-category web test | ~80 |
| `tests/test_categories.py`, `tests/test_e2e_phase1.py`, `tests/test_ingestion.py` | Modify | PATCH tests updated to use `data=...` instead of `json=...` | ~10 |

**No new migrations, no new models, no new endpoints.** PR #3 is purely UI + one Query filter on `list_transactions` + form-encoded body support on PATCH.

## Test strategy (target 13 new tests)

For the 4 list_transactions filter combos:
- `test_list_transactions_no_category_filter` — no filter, all transactions
- `test_list_transactions_category_id_filter` — `category_id=<uuid>` only, returns only matching
- `test_list_transactions_uncategorized_filter` — `uncategorized=true` only, returns only `category_id IS NULL OR low_confidence=True`
- `test_list_transactions_both_filters` — both, returns the union (parenthesized)

For the UI:
- `test_per_row_select_rendered_with_13_options` — the partial renders the `<select>` with 12 + blank
- `test_per_row_select_selected_option_matches_category_id` — the current `category_id` is `selected`
- `test_filter_form_has_multiselect_and_uncategorized_checkbox` — the form has both controls with the right labels
- `test_filter_form_submission_narrows_table` — submitting the form narrows the table
- `test_filter_form_submission_with_multiple_category_ids_narrows_table` — multi-category web filter
- `test_patch_with_empty_string_category_id_clears_fk` — PATCH with `category_id=""` (the "—" option) clears the FK

For the PATCH Accept header dispatch:
- `test_patch_with_accept_text_html_returns_html_partial` — PATCH returns `text/html` when `Accept: text/html`
- `test_patch_with_accept_application_json_returns_json` — PATCH returns `application/json` (default)

Total: 11 new tests + 2 multi-category web tests = 13. PR #3's test count delta is 366 + 13 = 379 (approximate; the exact count depends on the test file structure). Coverage target: ≥83.17% (the current baseline after PR #2's 0.75pp drop).

## Migration & Rollback

- **NO new migration** (PR #3 is UI-only).
- **NO new column** (the `category_id` and `low_confidence` columns are already in main from PR #2).
- **Rollback**: revert the commit. The 2 Query params on `list_transactions` are additive (no `WHERE` clause change if both are unset). The PATCH response change is additive (defaults to JSON). The template changes are localized to 2 files.

## Cherry-pick isolation gate

The diff vs `main` for `app/services/ingestion.py` MUST be **ZERO**. The 4 production files in scope are:
- `app/api/v1/transactions.py`
- `app/web/router.py`
- `app/web/templates/partials/transactions_table.html`
- `app/web/templates/transactions.html`

NO other production code is touched. The apply phase will verify this via `git diff main -- app/services/ingestion.py` showing zero changes.

## Out of Scope

- Bulk category assignment UI (decision #12 says anti-feature)
- Phase 2 PRs #4-#6 (Merchants + aliases, Recurring detection, Docs + e2e)
- The seeded `Uncategorized` rename (the seed has it at sort_order=11 with a real UUID; the "Untagged" filter is a DIFFERENT special case)
- The `_metadata_completeness` cherry-pick (already in main from PR #28, untouched by PR #3)
