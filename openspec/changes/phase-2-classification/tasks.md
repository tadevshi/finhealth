# Tasks: Phase 2 — Classification, PR #2 (Categories Foundation)

## Summary

- **~480 LOC** (250 prod + 230 test), **15 new tests**, **single PR**.
- Tests: 366 + 15 = **381** with the same 3 pre-existing failures (NOT regressions).
- Coverage target: **≥91.74%** (per spec), 80%+ achievable in this env.
- Source: engram memories #68 (proposal), #69 (spec), #70 (design), and
  the 14 product decisions in #53.

## Review Workload Forecast

- Decision needed before apply: **No**
- Chained PRs recommended: **No** (PR #2 is a single PR; PRs #3–#6 are
  the chained follow-ups)
- Chain strategy: N/A
- 400-line budget risk: **Low**

## Tasks

### Phase 1 — Foundation

- [ ] **1.1 Create migration 0005 (`alembic/versions/0005_phase2_categories.py`).**
  Revises `0004_timestamp_server_defaults`. Creates `categories` table with
  `id` (UUID PK, CHAR(36)), `created_at` / `updated_at` (timezone-aware, NOT
  NULL, `server_default=func.now()`), `name` (String(50), NOT NULL,
  UNIQUE), `display_name` (String(100), NOT NULL), `sort_order` (Integer,
  NOT NULL). Index `ix_categories_sort_order`. Seed the 12 Y-NAB categories
  via `op.bulk_insert` (see design §"Seeded Taxonomy"). Downgrade drops
  indexes + table.

- [ ] **1.2 Create migration 0006 (`alembic/versions/0006_phase2_category_columns.py`).**
  Revises `0005_phase2_categories`. **Partial migration** — PR #4 will extend
  this file with `merchants`, `merchant_aliases`, and `Transaction.merchant_id`.
  Add a header comment documenting the extension point. Add to
  `transactions`:
  - `category_id` (String(36), NULL, FK to `categories.id` ON DELETE SET NULL)
  - `low_confidence` (Boolean, NOT NULL, default `0`)
  - `ix_transactions_category_id` (B-tree on `category_id`).
  Downgrade drops the index + both columns in reverse order.

- [ ] **1.3 Add `Category` model (`app/models/category.py`) + extend
  `Transaction` (`app/models/transaction.py`).** `Category(UUIDMixin,
  TimestampMixin, Base)` mirrors `Bank`. Add `category_id` (UUIDType, FK,
  nullable, indexed) and `low_confidence` (Boolean, NOT NULL, default
  `False`) on `Transaction`. Add `category: Mapped[Category | None]` back-ref
  with `lazy="joined"`. Re-export `Category` in `app/models/__init__.py`.

- [ ] **1.4 Add Pydantic schemas (`app/schemas/domain.py` + re-export in
  `app/schemas/__init__.py`).** `CategoryResponse` (id, name, display_name,
  sort_order, timestamps, `from_attributes=True`). `CategoryRenameRequest`
  (`name?: str` min 1, max 50; `display_name?: str` min 1, max 100;
  `extra="forbid"`; at least one field supplied is enforced in the
  endpoint, not the schema). Extend `TransactionResponse` with
  `category_id: uuid.UUID | None` and `low_confidence: bool`. Extend
  `TransactionCategoryUpdate` (in `app/api/v1/transactions.py`) with
  `category_id: uuid.UUID | None` (field optional, extra="forbid").

### Phase 2 — LLM + Ingestion

- [ ] **2.1 Update prompts (`app/services/llm/prompts.py`).** Add a
  `SEED_CATEGORY_NAMES: Final = (...)` constant holding the 12 names. Update
  both NACIONAL and INTERNACIONAL prompt templates: the "INSTRUCTIONS"
  section adds an explicit "Use one of these 12 category names" paragraph
  that interpolates the list. Rewrite the few-shot examples
  (`_NACIONAL_EXAMPLE_OUTPUT`, `_INTERNACIONAL_EXAMPLE_OUTPUT`) so every
  `category` field is drawn from the set.

- [ ] **2.2 Validate category in ingestion (`app/services/ingestion.py`).**
  Add `from app.models.category import Category` import. In
  `_build_transactions`: at the top of the method, run
  `select(Category)` once, build `by_name: dict[str, Category]` keyed by
  `c.name.lower()`. For each transaction, look up `txn.category` after
  `strip().lower()`; hit → `(cat.id, cat.name, False)`, miss → `(None,
  txn.category or "Uncategorized", True)`. Set
  `Transaction(category=cat_name, category_id=cat_id,
  low_confidence=low_conf)`. No other lines in `_build_transactions` change.

### Phase 3 — API

- [ ] **3.1 New categories router (`app/api/v1/categories.py`).**
  `router = APIRouter(prefix="/categories", tags=["categories"])`.
  `GET ""` returns `[CategoryResponse]` ordered by `sort_order.asc()`.
  `POST "/{category_id}"` accepts `CategoryRenameRequest`, validates
  collision (proposed `name` taken by another row → 422), performs
  `Category` UPDATE + `Transaction` UPDATE (only on the matching
  `category_id`) in a single `session.commit()`. 404 when UUID is unknown.
  Re-export via `__all__ = ["router"]`. Wire into `app/api/v1/router.py`.

- [ ] **3.2 Extend PATCH endpoint (`app/api/v1/transactions.py`).** Update
  `update_transaction`: when `payload.category_id` is not None, look up
  the `Category` row (404 if missing), set `transaction.category_id` and
  `transaction.category = cat.name` and `low_confidence=False`. When
  `payload.category_id` is None and `payload.category` is set, set
  `transaction.category_id = None`, `transaction.category = payload.category`,
  `low_confidence = True`, and emit exactly one
  `logger.warning("TransactionCategoryUpdate deprecation: legacy `category: str` field used; ...")`
  (no log when `category_id` was set). Both branches share a single
  `session.commit()`.

### Phase 4 — Verification

- [ ] **4.1 Run the gates.** `pytest --no-cov` ≥381 passing + same 3 (or 22)
  pre-existing failures; no new failures. `ruff check .` clean. `ruff
  format --check <modified files>` clean. `mypy --strict app/` clean.
  Coverage ≥91.74% (or the current env's floor, whichever is higher).

- [ ] **4.2 Cherry-pick isolation gate.** `git diff main --stat` shows
  changes only in the files in the §"File Changes" list. `git diff main
  -- app/services/ingestion.py` shows ONLY the new `Category` import and
  the modified `_build_transactions` method. NONE of: chunk-loop
  changes, `try/finally`, `first_successful_chunk_seen`,
  `last_chunk_exc`, all-fail guard.

### Phase 5 — Commit Hygiene

- [ ] **5.1 Atomic conventional commits, no `Co-Authored-By`, buildable
  at each commit.** Suggested commit order: migration 0005 + Category
  model + Transaction extension; migration 0006 + schemas; prompts;
  ingestion validation; categories router; transactions PATCH
  extension; tests. Open the PR with `gh pr create` referencing the
  spec / design / tasks artifacts.
