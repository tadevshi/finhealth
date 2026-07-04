# phase2-categories

## Purpose

Delta spec for `phase-2-pr3-categories-ui` (Phase 2 PR #3 — Categories UI). PR #2 established the data model (Category, categories table, category_id FK, PATCH write-through). This delta adds the consumer-side surface: per-row `<select>` rendering, "Filter by category" multi-select widget, `list_transactions` Query filters, and PATCH Accept-header negotiation. The 5 existing requirements from PR #2 are unchanged. The delta only ADDS new behaviour.

## ADDED Requirements

### Requirement: Per-Row Category Edit Is a Server-Rendered `<select>`

The per-row category edit in `partials/transactions_table.html` MUST be a server-rendered `<select name="category_id">` (not a free-text `<input>`). The 13 `<option>`s are the 12 categories (sorted by `sort_order`) + a blank "—" for "no category". The currently-assigned `category_id` is rendered as the `selected` option. The `hx-patch="/api/v1/transactions/{id}"` wiring is unchanged. PATCH accepts form-encoded body (HTMX browser flow).

#### Scenario: `<select>` is rendered with 13 options

- GIVEN the transactions page is loaded
- WHEN the per-row category edit is rendered for a transaction with `category_id=groceries.id`
- THEN the `<select>` has 13 `<option>`s (12 categories + blank), and the `Groceries` option has `selected`

#### Scenario: PATCH round-trip with the new select

- GIVEN a transaction has `category_id=groceries.id` and the user changes the per-row select to `Dining Out`
- WHEN HTMX submits `PATCH /api/v1/transactions/{id}` with `category_id=dining_out.id`
- THEN the response is the partial row HTML with the new `selected` option, and the underlying transaction has `category_id=dining_out.id` and `category='Dining Out'`

#### Scenario: Defensive blank-selected for stale `category_id`

- GIVEN a transaction has `category_id=<stale_uuid>` (a UUID not in the seeded `categories` list, e.g., from a deleted/renamed category)
- WHEN the per-row category edit is rendered
- THEN the blank "—" option has `selected` (defensive fallback)

### Requirement: Filter Form Has a Multi-Select Category Widget and an "Untagged" Checkbox

The filter form in `transactions.html` MUST have a `<select multiple name="category_id">` for the 12 category UUIDs PLUS a `<input type="checkbox" name="uncategorized" value="true">` labeled "Untagged or low confidence" (to disambiguate from the seeded `Uncategorized` category which has a real UUID). The form serializes as `?category_id=<uuid1>&category_id=<uuid2>&uncategorized=true`.

#### Scenario: Multi-select rendered in the form

- GIVEN the filter form is loaded
- WHEN the user inspects the markup
- THEN there is a `<select multiple name="category_id">` with 12 `<option>`s (one per category, sorted by `sort_order`) AND a `<input type="checkbox" name="uncategorized" value="true">` with the label "Untagged or low confidence"

#### Scenario: Multi-select filter applied (narrow)

- GIVEN the user submits the form with `category_id=<groceries.id>&category_id=<dining_out.id>` (no `uncategorized`)
- WHEN the page reloads
- THEN the transactions list shows ONLY transactions with `category_id IN (groceries.id, dining_out.id)`

#### Scenario: "Untagged" checkbox filter applied (widen to NULL/low_confidence)

- GIVEN the user submits the form with `uncategorized=true` (no `category_id`)
- WHEN the page reloads
- THEN the transactions list shows ONLY transactions with `category_id IS NULL OR low_confidence=True`
- AND the seeded `Uncategorized` row is NOT auto-included (it has a real UUID; it's a category, not an "untagged" marker)

### Requirement: `list_transactions` Accepts `category_id` and `uncategorized` Query Filters

The `GET /api/v1/transactions` endpoint MUST accept two new Query params: `category_id: list[uuid.UUID] | None` and `uncategorized: bool = False`. The filter combination logic is:
- Neither set → no `WHERE` clause added (return all transactions)
- `category_id` only → `WHERE Transaction.category_id IN (<uuid1>, <uuid2>, ...)`
- `uncategorized` only → `WHERE (Transaction.category_id IS NULL OR Transaction.low_confidence IS TRUE)`
- Both set → `WHERE (Transaction.category_id IN (<uuid1>, ...) OR Transaction.category_id IS NULL OR Transaction.low_confidence IS TRUE)` (parenthesized `or_`)

#### Scenario: No filter returns all transactions

- GIVEN no `category_id` and no `uncategorized`
- WHEN `list_transactions` is called
- THEN all transactions are returned (no `WHERE` clause)

#### Scenario: `category_id` only filters to selected categories

- GIVEN `category_id=<groceries.id>&category_id=<dining_out.id>`
- WHEN `list_transactions` is called
- THEN ONLY transactions with `category_id IN (groceries.id, dining_out.id)` are returned

#### Scenario: `uncategorized` only returns NULL or low_confidence rows

- GIVEN `uncategorized=true`
- WHEN `list_transactions` is called
- THEN ONLY transactions with `category_id IS NULL OR low_confidence IS TRUE` are returned

#### Scenario: Both set returns the union (parenthesized)

- GIVEN `category_id=<groceries.id>&uncategorized=true`
- WHEN `list_transactions` is called
- THEN transactions with `category_id=groceries.id` OR with `category_id IS NULL OR low_confidence IS TRUE` are returned (the union, parenthesized)

### Requirement: `PATCH /api/v1/transactions/{id}` Returns `text/html` Partial When `Accept: text/html`

The PATCH endpoint MUST perform `Accept` header negotiation on the response:
- When the request has `Accept: text/html` (browser request via HTMX), the response is `text/html` containing the partial row HTML (the same partial used by `transactions_rows_partial`), with the new `category_id` rendered as `selected` in the `<select>`.
- When the request has `Accept: application/json` (default for non-browser clients), the response is `application/json` containing the `TransactionResponse` (unchanged from PR #2).
- The request handling is unchanged: still accepts `category_id: UUID | None` and the legacy `category: str` with the deprecation log. PATCH accepts both JSON (`application/json`) and form-encoded (`application/x-www-form-urlencoded`) bodies.

#### Scenario: PATCH with `Accept: text/html` returns HTML partial

- GIVEN a transaction with `category_id=groceries.id`
- WHEN the user submits HTMX `PATCH /api/v1/transactions/{id}` with `Accept: text/html` and `category_id=dining_out.id` (form-encoded)
- THEN the response is `text/html` containing the partial row with `<option value="<dining_out.id>" selected>Dining Out</option>`

#### Scenario: PATCH with `Accept: application/json` returns JSON (default)

- GIVEN the same transaction
- WHEN a non-browser client submits `PATCH /api/v1/transactions/{id}` with `Accept: application/json` (or no `Accept` header) and `category_id=dining_out.id`
- THEN the response is `application/json` containing the `TransactionResponse` with `category_id=dining_out.id`

#### Scenario: PATCH with `category_id` writes both columns (unchanged from PR #2)

- GIVEN the PATCH succeeds
- WHEN the transaction is re-fetched
- THEN `category_id=dining_out.id` AND `category='Dining Out'` (write-through per PR #2 spec requirement 5)
