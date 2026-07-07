# phase2-categories

## Purpose

Phase 1 lets the LLM emit a free-text `category` string per transaction and lets the user
type any string in `PATCH /api/v1/transactions/{id}`. There is no taxonomy, no
validation, and no consistency between rows from the same statement.

`phase2-categories` introduces a closed set of 12 flat Y-NAB categories, seeded at
migration time, and enforced at three boundaries:

1. **LLM extraction** — the prompt lists the 12 names verbatim and the few-shot
   examples use them. A `category` value that is not in the set becomes a miss,
   not a pass-through.
2. **Ingestion** — `_build_transactions` looks up the emitted name against the
   seed (one query + in-memory dict cache). A hit sets `category_id` and
   `category=cat.name` with `low_confidence=False`. A miss sets
   `category_id=NULL` and preserves the LLM string (or `"Uncategorized"` when
   the LLM did not emit anything) with `low_confidence=True`.
3. **API** — `PATCH /api/v1/transactions/{id}` accepts the new `category_id`
   field with a write-through to the denormalized `category` string. The legacy
   `category: str` field is kept working but emits a deprecation log so the
   client can be migrated to the new contract. `POST /api/v1/categories/{id}`
   is a stateless rename that propagates the change to every transaction row
   in a single transaction.

Out of scope: category hierarchy, bulk category assignment from a filter view,
Sentry alerts for failures, audit trail for renames.


> **Phase 2 PR #4 extends the partial migration `0006_phase2_merchants_transactions_alter.py`**
> (originally started in PR #2) with the `merchants` + `merchant_aliases` tables and the
> `transactions.merchant_id` FK. The capability's behaviour (9 requirements, 22
> scenarios from PR #2 + PR #3) is unchanged; only the schema is extended. The new
> merchant-related behavior is documented in the `phase2-merchant-aliasing` capability
> (a sibling spec).

## ADDED Requirements

### Requirement: Seeded Taxonomy and FK Columns

The system MUST provide a closed set of 12 flat categories seeded at migration
time, and every `Transaction` row MUST carry a nullable `category_id` FK and a
non-null `low_confidence` Boolean. The category lookup MUST be case-insensitive
on `strip().lower()` of the LLM's emitted `category` string. (Decision #1, #8)

#### Scenario: GET /api/v1/categories returns 12 rows in sort_order

- **GIVEN** the database has been migrated to head
- **WHEN** the client calls `GET /api/v1/categories`
- **THEN** the response is a JSON array of 12 categories in ascending `sort_order`
- **AND** every entry carries `id`, `name`, `display_name`, `sort_order`,
  `created_at`, `updated_at`
- **AND** the 12 names are the Y-NAB-derived list agreed in decision #1

### Requirement: LLM Emits Category from the Closed Set

The extraction prompt MUST include the 12 category names verbatim and the
few-shot examples MUST use names from the set. (Decision #1, #2)

#### Scenario: build_extraction_prompt output contains all 12 names

- **GIVEN** the prompt module is loaded
- **WHEN** the test calls `build_extraction_prompt("NACIONAL", "")` and asserts
  on the rendered string
- **THEN** every one of the 12 seeded category names appears at least once in
  the rendered prompt
- **AND** the few-shot example output for NACIONAL uses names from the set
- **AND** the few-shot example output for INTERNACIONAL uses names from the set

### Requirement: Ingestion Validates Category Against the Seed

`_build_transactions` MUST resolve the LLM's emitted `category` string against
the seeded set in a single query, then build a `Transaction` row whose
`category_id`, `category`, and `low_confidence` reflect the result. A hit sets
`category_id=cat.id`, `category=cat.name`, `low_confidence=False`. A miss sets
`category_id=NULL`, preserves the LLM's string in `category` (or
`"Uncategorized"` when the LLM emitted `None` or an empty string), and sets
`low_confidence=True`. (Decision #1, #2, #8)

#### Scenario: Hit on a valid closed-set name

- **GIVEN** the seed is migrated and the LLM emits a transaction with
  `category="Food"`
- **WHEN** the ingestion runs `_build_transactions`
- **THEN** the resulting `Transaction` row has
  `category_id=<seed.Food.id>`, `category="Food"`, `low_confidence=False`

#### Scenario: Miss on a name not in the closed set

- **GIVEN** the seed is migrated and the LLM emits a transaction with
  `category="PetStore"` (not in the set)
- **WHEN** the ingestion runs `_build_transactions`
- **THEN** the resulting `Transaction` row has `category_id=NULL`,
  `category="PetStore"`, `low_confidence=True`

#### Scenario: Miss on a null or empty category

- **GIVEN** the seed is migrated and the LLM emits a transaction with
  `category=None` (or `""`)
- **WHEN** the ingestion runs `_build_transactions`
- **THEN** the resulting `Transaction` row has `category_id=NULL`,
  `category="Uncategorized"`, `low_confidence=True`

### Requirement: PATCH Endpoint Accepts category_id with Write-Through

`PATCH /api/v1/transactions/{id}` MUST accept an optional `category_id: UUID`
field. When `category_id` is set, the endpoint MUST write the FK and the
denormalized `category` string in a single transaction. When the legacy
`category: str` field is supplied (and `category_id` is omitted), the endpoint
MUST write the string, leave `category_id=NULL`, set `low_confidence=True`,
and emit a single `logger.warning` documenting the deprecation. (Decision #8)

#### Scenario: PATCH with category_id writes FK + denormalized string

- **GIVEN** a transaction exists and a valid `Category` row exists
- **WHEN** the client calls `PATCH /api/v1/transactions/{id}` with
  `{"category_id": "<cat.id>"}`
- **THEN** the response is 200 with the updated transaction
- **AND** the row's `category_id` is the supplied UUID
- **AND** the row's `category` is the matching `Category.name`
- **AND** the row's `low_confidence` is `False`

#### Scenario: PATCH with legacy category: str sets low_confidence=True

- **GIVEN** a transaction exists
- **WHEN** the client calls `PATCH /api/v1/transactions/{id}` with
  `{"category": "Custom Label"}` (no `category_id`)
- **THEN** the response is 200 with the updated transaction
- **AND** the row's `category_id` is `NULL`
- **AND** the row's `category` is `"Custom Label"`
- **AND** the row's `low_confidence` is `True`
- **AND** the test asserts exactly one `WARNING` log line is emitted on
  `app.api.v1.transactions` mentioning the deprecation

### Requirement: Stateless Category Rename Endpoint

`POST /api/v1/categories/{id}` MUST accept an optional `name` and an optional
`display_name`. The endpoint MUST validate the new `name` (or `display_name`)
and propagate the change to every `Transaction` row whose `category_id` matches
the renamed category, in a single `session.commit()`. The endpoint MUST
respond 404 when the UUID does not exist, and 422 when the proposed `name`
collides with another category's `name`. (Decision #8, #11)

#### Scenario: Rename propagates to transactions atomically

- **GIVEN** category `Food` has 3 transactions
- **WHEN** the client calls `POST /api/v1/categories/{id]` with
  `{"name": "Groceries", "display_name": "Groceries & Food"}`
- **THEN** the response is 200 with the renamed category
- **AND** the 3 transactions' `category` strings are updated to `"Groceries"`
- **AND** their `category_id` values are unchanged
- **AND** the change is committed in a single transaction (the rename UPDATE
  and the transactions UPDATE are atomic)

#### Scenario: Rename collision returns 422

- **GIVEN** two categories exist, `Food` and `Transport`
- **WHEN** the client calls `POST /api/v1/categories/<food.id>` with
  `{"name": "Transport"}`
- **THEN** the response is 422
- **AND** the `Food` row is unchanged
- **AND** no transaction rows are touched

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
