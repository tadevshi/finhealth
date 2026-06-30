# Design: phase-2-classification PR #2 (Categories Foundation)

## What

Technical design for **PR #2 (Categories Foundation)** of the
`phase-2-classification` SDD change. PR #2 introduces the 12-category Y-NAB
taxonomy, the FK columns on `Transaction`, the LLM closed-set prompt, the
ingestion-side validation, the `PATCH` write-through, and the stateless rename
endpoint. PRs #3–#6 build on this foundation; this design only describes PR
#2.

## Why

Phase 1's `Transaction.category` is a free-text string. The LLM emits it, the
user can type any string, and there is no validation. This is the single
biggest correctness hole in the application: the user cannot trust a "by
category" view because two rows from the same statement may carry different
labels for the same merchant. PR #2 closes that hole with a closed-set
taxonomy enforced at the LLM, the ingestion, and the API.

## Where

- `app/models/category.py` (new) — `Category` ORM model.
- `app/models/transaction.py` (modified) — `category_id` FK, `low_confidence`
  Boolean, relationship.
- `app/schemas/domain.py` (modified) — `CategoryResponse`,
  `CategoryRenameRequest`, plus `category_id` and `low_confidence` on
  `TransactionResponse`.
- `app/services/llm/prompts.py` (modified) — closed-set enumeration + canonical
  few-shot examples.
- `app/services/ingestion.py` (modified) — new import + `_validate_category`
  helper, called from `_build_transactions`.
- `app/api/v1/categories.py` (new) — `GET /api/v1/categories` and
  `POST /api/v1/categories/{id}`.
- `app/api/v1/router.py` (modified) — wire the new router.
- `app/api/v1/transactions.py` (modified) — `TransactionCategoryUpdate`
  extended with `category_id`; legacy `category: str` path emits a deprecation
  `logger.warning`.
- `alembic/versions/0005_phase2_categories.py` (new) — `categories` table +
  seed of 12 rows.
- `alembic/versions/0006_phase2_category_columns.py` (new) — `category_id` FK,
  `low_confidence` Boolean, `ix_transactions_category_id` index. Migration 0006
  is partial — PR #4 will extend it with `merchants`, `merchant_aliases`, and
  `Transaction.merchant_id`.
- `tests/test_categories.py` (new) — model, list endpoint, rename endpoint,
  PATCH write-through, deprecation log.
- `tests/test_ingestion.py` (modified) — closed-set validation, miss / hit /
  null paths, atomicity.
- `tests/test_prompts.py` (new) — prompt contains all 12 names; few-shot uses
  closed set.
- `tests/test_alembic.py` (modified) — categories table + seed + new columns
  round-trip.

## Key Decisions (10 Locked)

1. **Single migration 0006 file (PR #2 starts, PR #4 extends).** Avoids two
   batched round-trips on the same table; the file is shared with PR #4 via a
   documented extension point.
2. **Seed via `op.bulk_insert` in `upgrade()`.** Atomic with the table create;
   one transaction.
3. **One query + in-memory dict cache for category lookup.** Avoids N+1 in
   `_build_transactions` — one SELECT at the start of the call, dict lookup
   per row.
4. **`strip().lower()` exact match.** Fuzzy is overkill for 12 items and the
   LLM is told to emit the canonical spelling.
5. **Legacy PATCH `category: str` sets `low_confidence=True`.** Per spec
   scenario 6 — the user is bypassing the taxonomy, so the row is flagged.
6. **Rename payload `{name?, display_name?}` (both optional).** Flexibility
   for the UI to rename one or both.
7. **Single `session.commit()` for rename atomicity.** UPDATE on the category
   row + UPDATE on every matching transaction in one COMMIT.
8. **Simple B-tree index on `category_id`.** Primary access pattern is
   `WHERE category_id = ?` (filter by category).
9. **Cherry-pick isolation gate enforced on `ingestion.py` diff.** Only the
   new import + `_build_transactions` body; no chunk-loop, counter, or
   `try/finally` changes leak in.
10. **Schemas in `app/schemas/domain.py`.** Follows the existing single-file
    pattern (no `app/schemas/categories.py`).

## Architecture

### Data Model

```
categories                       transactions
+---------+   1  +---------+   1  +--------------+
| id (PK) | <--- |  ...    | <--- | id (PK)      |
| name    |  FK  | ...     |  FK  | category_id  |
| display |      | ...     |      | low_conf.    |
| sort    |      | ...     |      | category     |
| ts cols |      | ...     |      | (denorm.)    |
+---------+      +---------+      +--------------+
```

- `Category(UUIDMixin, TimestampMixin, Base)` — same shape as `Bank`.
- `Transaction.category_id` — nullable FK to `categories.id`, indexed.
- `Transaction.low_confidence` — `Boolean NOT NULL DEFAULT 0`. New rows default
  to `False`; legacy rows backfill to `False` because the migration is
  additive.
- `Transaction.category` — the denormalized string, kept. Existing rows are
  untouched; the column is the write-through target on PATCH and rename.

### Seeded Taxonomy (12 Y-NAB-Derived)

The 12 names are taken from the project's product decisions (decision #1).
They are stored in the migration's `upgrade()` body via `op.bulk_insert` so a
fresh `alembic upgrade head` produces a fully populated table.

| sort_order | name           | display_name                |
|-----------:|----------------|-----------------------------|
| 1          | Food           | Food & Dining               |
| 2          | Groceries      | Groceries                   |
| 3          | Transport      | Transport                   |
| 4          | Shopping       | Shopping                    |
| 5          | Entertainment  | Entertainment               |
| 6          | Bills          | Bills & Utilities           |
| 7          | Health         | Health & Medical            |
| 8          | Travel         | Travel                      |
| 9          | Subscriptions  | Subscriptions               |
| 10         | Services       | Services                    |
| 11         | Transfers      | Transfers                   |
| 12         | Other          | Other                       |

### Ingestion Validation Flow

```python
def _build_transactions(...):
    # Local import to keep module-level import surface small.
    from app.models.category import Category

    # One query at the start of the call, used as a dict cache.
    result = await self._session.execute(select(Category))
    by_name = {c.name.lower(): c for c in result.scalars()}

    transactions: list[Transaction] = []
    for index, txn in enumerate(extraction.transactions):
        ...
        cat_id, cat_name, low_conf = _resolve_category(txn.category, by_name)
        transactions.append(Transaction(
            ...,
            category=cat_name,
            category_id=cat_id,
            low_confidence=low_conf,
        ))
```

`_resolve_category` does the case-insensitive lookup against the dict cache,
returning the tuple `(cat_id, cat_name, low_conf)`. The case-insensitive
match is `strip().lower()` against `Category.name.lower()`. Misses return
`(None, original_or_uncategorized, True)`.

### API Surface

| Method | Path                              | Body                                  | Response             |
|--------|-----------------------------------|---------------------------------------|----------------------|
| GET    | `/api/v1/categories`              | —                                     | `[CategoryResponse]` |
| POST   | `/api/v1/categories/{id}`         | `{name?, display_name?}`              | `CategoryResponse`   |
| PATCH  | `/api/v1/transactions/{id}`       | `{category_id?, category?}` (legacy)   | `TransactionResponse`|

`POST /api/v1/categories/{id}` is stateless. It performs two UPDATEs in a
single `session.commit()`:

1. Update `Category.name` / `display_name` (only the fields supplied).
2. Update every `Transaction` row whose `category_id` matches the supplied
   UUID, setting `Transaction.category` to the new `Category.name`.

The collision check (proposed `name` already taken by another row) is
enforced inside the endpoint before the UPDATE; the response is 422.

### LLM Prompt Update

The `NACIONAL` and `INTERNACIONAL` prompt templates are updated in two places:

1. The "INSTRUCTIONS" section adds an explicit "Use one of these 12 category
   names" paragraph listing the seeded taxonomy.
2. The few-shot examples (`_NACIONAL_EXAMPLE_OUTPUT`,
   `_INTERNACIONAL_EXAMPLE_OUTPUT`) are rewritten so every `category` field
   is drawn from the closed set.

The list is inlined verbatim — the prompt module imports it from the
`Category` model or a shared constant so a seed change in a future PR
re-renders the prompt without a code edit. (For PR #2, the list is hard-coded
in the prompt module; the migration 0006 is the source of truth for the
seed.)

## File Changes

- **New (6 files):**
  - `app/models/category.py`
  - `app/api/v1/categories.py`
  - `alembic/versions/0005_phase2_categories.py`
  - `alembic/versions/0006_phase2_category_columns.py`
  - `tests/test_categories.py`
  - `tests/test_prompts.py`
- **Modified (5 files):**
  - `app/models/transaction.py` (+ `category_id` FK, `low_confidence`, rel).
  - `app/models/__init__.py` (re-export `Category`).
  - `app/schemas/domain.py` (`CategoryResponse`, `CategoryRenameRequest`,
    `TransactionResponse` extension).
  - `app/schemas/__init__.py` (re-export new schemas).
  - `app/services/llm/prompts.py` (closed-set instruction + few-shot rewrite).
  - `app/services/ingestion.py` (new import + `_validate_category` helper +
    `_build_transactions` body change).
  - `app/api/v1/router.py` (include the new router).
  - `app/api/v1/transactions.py` (`TransactionCategoryUpdate` extended; legacy
    path emits deprecation log).
  - `tests/test_ingestion.py` (closed-set tests).
  - `tests/test_alembic.py` (categories seed + new columns round-trip).

## Test Strategy

15 new tests across 5 files (target: 366 → 381 passing):

| File                  | New tests | Notes |
|-----------------------|----------:|-------|
| `tests/test_categories.py` | 8 | model, list, rename, collision 422, PATCH write-through, PATCH legacy deprecation |
| `tests/test_prompts.py`    | 2 | NACIONAL prompt has all 12; INTERNACIONAL prompt has all 12; few-shot uses closed set |
| `tests/test_ingestion.py`  | 3 | closed-set hit, miss on unknown, miss on null |
| `tests/test_alembic.py`    | 2 | categories table + seed; new columns round-trip |

## Risk and Mitigations

- **Migration 0006 partial**: PR #4 must rebase onto this branch and extend
  the file. Mitigated by a header comment in 0006 pointing to PR #4.
- **Closed-set string drift**: a future PR may change the 12 names. The
  prompt module hard-codes the list for PR #2; PR #4 or later is the right
  time to centralise the constant.
- **N+1 in `_build_transactions`**: avoided by the one-query + dict cache
  pattern; the test asserts that no per-row query happens.

## Out of Scope (Handed Off)

- `GET /api/v1/transactions` filter by `category_id` (PR #3).
- "Uncategorized" filter (`category_id IS NULL OR low_confidence=True`)
  (PR #3).
- UI `<select>` and multi-select filter (PR #3).
- Merchant canonicalization, alias table, LLM helper (PR #4).
- Recurring detection, `recurring_rules` table, `is_active` override (PR #5).
- E2E + docs (PR #6).
