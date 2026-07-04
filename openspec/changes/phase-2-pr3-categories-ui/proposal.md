# Proposal: Phase 2 PR #3 — Categories UI

## Intent

PR #2 delivered the closed-set taxonomy (12 Y-NAB categories, `category_id` FK, `PATCH` write-through). The UI still shows a free-text `<input>` for every transaction row — no validation, no picker, no filter by category. PR #3 replaces that `<input>` with a server-rendered `<select>` sourced from `GET /api/v1/categories`, adds a "Filter by category" multi-select widget to the transactions filter form (per decision #12), and threads `category_id` + `uncategorized` Query filters through `list_transactions`. The change is pure UI plus one Query-filter addition — no new migration, no new model, no new column.

## Scope

### In Scope

- **`app/api/v1/transactions.py:list_transactions`** — add `category_id: list[uuid.UUID] | None` + `uncategorized: bool` Query params; build parenthesized `or_` SQL clause (architecture pick C1).
- **`app/api/v1/transactions.py:update_transaction` (PATCH)** — add `Accept` header negotiation: return `text/html` (partial row) when `Accept: text/html` (browser), `application/json` (`TransactionResponse`) otherwise (architecture pick B1). PATCH also accepts form-encoded body (`application/x-www-form-urlencoded`) per the judgment-day Round 1 Fix 1 (the per-row `<select>` is HTMX-driven and sends form-encoded by default).
- **`app/web/router.py`** — thread the 2 new Query params through `transactions_page` + `transactions_rows_partial`; add `categories: list[Category]` to page context; extract the `_query_transactions` helper per design decision D2 (addresses the existing TODO at L164-165).
- **`app/web/templates/partials/transactions_table.html`** — replace `<input type="text" name="category">` with server-rendered `<select name="category_id">` (13 `<option>`s: 12 categories sorted by `sort_order` + blank "—"). The blank option is `selected` defensively when the transaction's `category_id` is not in the seeded list (judgment-day Round 1 Fix 6).
- **`app/web/templates/transactions.html`** — add `<select multiple name="category_id">` for 12 UUIDs + `<input type="checkbox" name="uncategorized" value="true">` labeled "Untagged or low confidence" (architecture pick A1, design decision D3).
- **Tests** — 13 new tests: 4 filter-branch tests on `list_transactions`, 1 multi-select web test (judgment-day Fix 2), 6 web UI tests (`<select>` in partial, multi-select in form, filter combo, PATCH HTML response, PATCH JSON default, uncategorized checkbox), 1 empty-string PATCH test, 1 multi-category web test.

### Out of Scope

- Bulk category assignment UI (anti-feature per decision #12)
- Filter widget styling / CSS (follow existing project pattern)
- Phase 2 PRs #4-#6 (Merchants, Recurring, Docs)
- Renaming the seeded `Uncategorized` category (real UUID; distinct from the "Untagged" filter)
- Cherry-pick isolation to `app/services/ingestion.py` (zero diff expected — PR #3 touches only the API/web layer)
- Coverage recovery beyond the `list_transactions` filter branches (the round-1 fixes already pushed coverage to 87.34%)

## Capabilities

### Modified Capabilities

- **`phase2-categories`**: `list_transactions` gains `category_id` (multi-value UUID) and `uncategorized` (boolean) Query filters with parenthesized `or_` SQL semantics. `PATCH /transactions/{id}` gains `Accept: text/html` response negotiation AND form-encoded body support; the default `application/json` JSON contract is preserved. The web layer (`transactions_page` + partial) threads the same filters and renders categories from server context.

### New Capabilities

None. All capability-level behaviour is an extension of the `phase2-categories` spec; no new domain is introduced.

## Approach

Three architecture picks (locked by user, from explore synthesis `sdd/phase-2-pr3-categories-ui/explore`):

- **A1 — Uncategorized sentinel**: `<select multiple name="category_id">` for the 12 UUIDs + separate `<input type="checkbox" name="uncategorized" value="true">`. The handler builds `or_(category_id IN (...), (category_id IS NULL OR low_confidence=True))`.
- **B1 — In-place `<select>`**: server-rendered `<option>`s from page context; HTMX `hx-patch` unchanged. PATCH returns `text/html` (partial row) when `Accept: text/html`; defaults to `application/json` for non-browser clients.
- **C1 — Query filter**: `category_id: list[uuid.UUID] | None = Query(default=None)` + `uncategorized: bool = Query(default=False)`. Native FastAPI, idiomatic, typed.

File-by-file summary: 4 modified production files (~80-120 LOC), 1-2 modified test files (~100-130 LOC), zero new production files. The `_query_transactions` helper extraction (router.py L164-165 TODO) is included per design decision D2 — ~10 extra LOC with a testability win.

## Affected Areas

| Area | Impact | Description |
|------|--------|-------------|
| `app/api/v1/transactions.py` | Modified | 2 new Query params on `list_transactions`; `Accept` header negotiation on PATCH; form-encoded body support |
| `app/web/router.py` | Modified | 2 new Query params on page + partial; `categories` context; `_query_transactions` helper extraction |
| `app/web/templates/partials/transactions_table.html` | Modified | `<input>` → `<select>` for per-row category edit; defensive blank-selected for stale `category_id` |
| `app/web/templates/transactions.html` | Modified | Multi-select + checkbox in filter form; redundant `\|default` filter removed |
| `tests/test_transactions.py` (new) | New | 4 filter-branch tests + 1 empty-string PATCH test |
| `tests/test_web_phase1.py` (modified) | Modified | 6 web UI tests + 1 multi-category web test |
| `tests/test_categories.py`, `tests/test_e2e_phase1.py`, `tests/test_ingestion.py` | Modified | PATCH tests updated to use `data=...` instead of `json=...` (form-encoded support) |

## Risks

| Risk | Likelihood | Mitigation |
|------|------------|------------|
| **HTMX partial swap on PATCH** — JSON response leaves old `<option>` selected after swap | Medium | Accept header negotiation (B1): PATCH returns `text/html` partial for browser requests |
| **Form serialization of multi-select** — FastAPI `list[uuid.UUID]` must handle repeated Query params from `<select multiple>` | Low | Verify in test (judgment-day Round 1 Fix 2 added a multi-category web test); FastAPI natively supports this pattern |
| **Coverage regression** — PR #2 dropped coverage 0.75pp (83.92% → 83.17%) | Low | 4 filter-branch tests on `list_transactions` cover all `or_` combinations; round-1 fixes brought coverage to 87.34% (above the 83.17% floor) |
| **Duplicate query chain** — `transactions_page` + `transactions_rows_partial` duplicate the filter chain, doubling the change surface | Low | `_query_transactions` helper extraction (design decision D2) reduces duplication |
| **Seeded `Uncategorized` vs. filter marker** — same name, different semantics (real UUID vs. `IS NULL OR low_confidence=True`) | Low | Filter checkbox labeled "Untagged or low confidence" to disambiguate (design decision D3) |

## Rollback Plan

Revert the commit. The 2 Query params are additive (no WHERE clause change when both unset). The PATCH response defaults to JSON when no `Accept: text/html` header is present. Template changes are localized to 2 files with no migration dependency.

## Dependencies

- **PR #2 (Categories Foundation)** — already merged on `main` at `1ff5316`. Provides `Category` model, `categories` table (12 seeded), `GET /api/v1/categories`, `PATCH` write-through.
- **Decision #12** (engram `sdd/finhealth-phase2/decisions`) — per-item `<select>` is primary path; multi-select is for filtering only; bulk assignment is anti-feature.
- **5 design decisions** (D1-D5) — locked in the design sub-agent's synthesis; all honored in the implementation.

## Success Criteria

- [x] `GET /transactions?category_id=<uuid>&category_id=<uuid>&uncategorized=true` filters correctly (4 filter combinations pass)
- [x] Per-row `<select>` renders all 12 categories from server context
- [x] PATCH returns `text/html` partial when `Accept: text/html` (HTMX swap updates the selected `<option>`)
- [x] PATCH returns `application/json` TransactionResponse when no `Accept` header (API contract preserved)
- [x] PATCH accepts form-encoded body (HTMX browser flow works)
- [x] Cherry-pick isolation gate holds: `git diff main -- app/services/ingestion.py` is empty
- [x] 13 new tests pass; coverage 87.34% (above the 83.17% floor)
- [x] ruff + mypy clean
