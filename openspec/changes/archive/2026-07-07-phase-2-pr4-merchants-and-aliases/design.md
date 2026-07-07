# Design: Merchants + Aliases (Phase 2 PR #4)

## Context

Bank statements emit free-text `description` strings ("MCDONALDS SUC 12", "S.A. PARIS 03/06") with branch identifiers, legal suffixes, and installment markers. Without a canonical merchant entity, the user cannot answer "what did I spend at Lider?" without scanning every row. PR #2 (Categories Foundation, in main at `1ff5316`) introduced the `Category` model, the 12 Y-NAB taxonomy, and the `low_confidence` Boolean. PR #3 (Categories UI, in main) added the per-row `<select>` and category filter. PR #4 builds on both: it introduces `merchants` + `merchant_aliases` tables, a deterministic normalizer, an opt-in LLM helper, and two API endpoints.

The cherry-pick isolation gate from PR #2 remains in force: the chunk loop (`ingestion.py:483-566`), `try/finally`, counters, and `_metadata_completeness` are **untouched**. The merchant normalization is additive-only inside `_build_transactions` (~30 LOC). The LLM prompt, LLM clients, and LLM schemas are also untouched — normalization operates on the post-LLM `txn.description` string.

Migration 0006 is a single-file coordination point declared in PR #2's docstring. PR #4 extends the same file (no new migration) with the two new tables and the `transactions.merchant_id` FK.

## Goals & Non-Goals

**Goals**:
- Auto-merge ~80% of Chilean bank descriptions into canonical merchants via deterministic normalization
- Provide alias-table lookup (hit-or-create) so repeat descriptions bind to existing merchants
- Ship an opt-in LLM helper (default off) for ambiguous descriptions, bounded to first-occurrence-only
- Expose `GET /api/v1/merchants` and `POST /api/v1/merchants/{id}/aliases` endpoints
- Reuse `low_confidence` as the unified uncertainty signal (category miss OR merchant miss)

**Non-Goals**:
- Bulk merchant assignment UI (anti-feature per decision #4)
- LLM prompt change (normalization is server-side)
- `KNOWN_MERCHANT_PATTERNS` admin endpoint (hardcoded dict in v1)
- Phase 2 PRs #5-#6

## Approach

### Data Model

```
merchants                          merchant_aliases
┌──────────────────────┐           ┌──────────────────────────┐
│ id (UUID PK)         │           │ id (UUID PK)             │
│ name (VARCHAR 100)   │◄──┐       │ merchant_id (FK CASCADE) │
│   UNIQUE, INDEXED    │   │       │ alias_text (VARCHAR 200) │
│ default_category_id  │   │       │   UNIQUE                 │
│   (FK→categories.id  │   └───────│ normalized (VARCHAR 200) │
│    SET NULL, NULLABLE)│           │   INDEXED (non-unique)   │
│ is_active (BOOL)     │           │ source (VARCHAR 16)      │
│ created_at           │           │   'auto'|'user'|'llm'    │
│ updated_at           │           │ confidence (REAL, NULL)  │
└──────────────────────┘           │ created_at, updated_at   │
                                   └──────────────────────────┘

transactions (existing, extended)
┌──────────────────────────┐
│ ... (existing columns)   │
│ merchant_id (FK→merchants│
│   .id, SET NULL, NULLABLE│
│   INDEXED)  ← NEW        │
└──────────────────────────┘
```

`Merchant` and `MerchantAlias` follow the `UUIDMixin` + `TimestampMixin` pattern from `Category` (`app/models/category.py:37-78`). The `MerchantAliasSource` enum (`auto`, `user`, `llm`) is a Python `str` enum stored as `VARCHAR(16)` — same pattern as `StatementStatus`.

### KNOWN_MERCHANT_PATTERNS (D1)

Module-level constant in `app/services/merchants.py`. 12 Chilean merchants covering ~70-80% of typical bank statement volume:

| Canonical name | Default category | Alias patterns (raw) |
|---|---|---|
| `mcdonalds` | Dining Out | `MCDONALDS SUC *`, `MC DONALDS` |
| `lider` | Groceries | `LIDER COM *`, `LIDER EXPRESS` |
| `paris` | Shopping | `PARIS S.A.`, `PARIS */*` |
| `sodimac` | Shopping | `SODIMAC`, `SODIMAC HOME CENTER` |
| `easy` | Shopping | `EASY S.A.`, `EASY SUC *` |
| `starbucks` | Dining Out | `STARBUCKS`, `STARBUCKS COFFEE` |
| `copec` | Transportation | `COPEC`, `COPEC FULL` |
| `shell` | Transportation | `SHELL`, `SHELL V-POWER` |
| `uber` | Transportation | `UBER`, `UBER TRIP`, `UBER EATS` |
| `netflix` | Subscriptions | `NETFLIX`, `NETFLIX.COM` |
| `spotify` | Subscriptions | `SPOTIFY`, `SPOTIFY PREMIUM` |
| `amazon` | Shopping | `AMAZON`, `AMAZON.COM`, `AMAZON PRIME` |

The dict maps `normalized_text → category_name`. At resolve time, the normalizer looks up the category by name from the `categories_by_name` cache (same dict already built in `_build_transactions:730-733`).

### MerchantNormalizer Service

`app/services/merchants.py` — three methods:

1. **`normalize(raw: str) -> str`** — pure function. Lowercase → strip accents (`unicodedata.normalize('NFKD')`) → single `re.sub` with `\b`-anchored alternation for `S\.A\.|LTDA|CIA|SUCURSAL|SUC\b|\bCOM\b` → strip digits → strip punctuation (`/;.,`) → collapse whitespace.

2. **`resolve_merchant(session, description, categories_by_name) -> Merchant`** — computes `normalized = normalize(description)`, queries `merchant_aliases` by `normalized` (indexed lookup), returns existing `Merchant` on hit. On miss: checks `KNOWN_MERCHANT_PATTERNS[normalized]` for a default category, creates `Merchant` + `MerchantAlias(source='auto')` in same session, returns new merchant. Race-condition guard: try/except on `IntegrityError` for the alias insert (D3).

3. **`resolve_merchant_with_llm(session, description, llm_provider) -> Merchant`** — opt-in path. Called only when `LLM_MERCHANT_NORMALIZATION_ENABLED=true` AND the deterministic path produced a miss. Uses existing `LLMProvider` protocol with a dedicated prompt template. Result cached with `source='llm'` and `confidence=<score>`.

### Ingestion Integration

In `_build_transactions` (after the existing category resolution at lines 770-787):

```python
# One query for merchant_aliases cache (mirrors categories pattern)
aliases_by_normalized: dict[str, MerchantAlias] = {}
alias_result = await self._session.execute(select(MerchantAlias))
for alias in alias_result.scalars():
    aliases_by_normalized[alias.normalized] = alias

# Per-row (after category resolution):
normalized = normalize(txn.description)
merchant_id: uuid.UUID | None = None
alias = aliases_by_normalized.get(normalized)
if alias is not None:
    merchant_id = alias.merchant_id
else:
    merchant = await normalizer.resolve_merchant(
        self._session, txn.description, categories_by_name
    )
    merchant_id = merchant.id
    low_confidence = True  # OR semantics (D2)
```

The `low_confidence` flag uses OR semantics: `category_miss OR merchant_miss`. The existing category resolution sets it; the merchant resolution can only flip it to `True` (never back to `False`).

### API Endpoints

`app/api/v1/merchants.py` — mirrors `app/api/v1/categories.py` patterns:

- **`GET /api/v1/merchants`** — `select(Merchant).order_by(Merchant.name.asc())`, returns `list[MerchantResponse]`.
- **`POST /api/v1/merchants/{merchant_id}/aliases`** — accepts `MerchantAliasCreate(alias_text)`, validates merchant exists (404 if not), checks `UNIQUE(alias_text)` collision (422 if duplicate), creates `MerchantAlias(source='user', normalized=normalize(alias_text))`, single `commit()`.

## Decisions

### D1 — KNOWN_MERCHANT_PATTERNS contents

**Choice**: 12 Chilean merchants (table above) covering Dining Out, Groceries, Shopping, Transportation, Subscriptions.
**Rationale**: Covers ~70-80% of typical statement volume. Hardcoded dict is simple and version-controlled; extensible via follow-up PR if runtime editing is needed.

### D2 — low_confidence OR semantics

**Choice**: `low_confidence = category_miss OR merchant_miss`. Single Boolean, no second flag.
**Rationale**: The flag means "this row's tags are uncertain" — exactly what the user needs for bulk review. The user can determine which miss caused it by joining the merchant (`is_active=False` if just auto-created, `default_category_id IS NULL` if unknown pattern).

### D3 — Alias lookup race condition mitigation

**Choice**: Try/except on `IntegrityError` for the alias insert. On collision, rollback and re-query the existing alias.
**Rationale**: More portable than `SELECT ... FOR UPDATE` (SQL-dialect-specific). For a single-user personal-finance app, concurrent ingests are negligible; this is a defensive measure.

### D4 — KNOWN_MERCHANT_PATTERNS location

**Choice**: Module-level constant in `app/services/merchants.py`.
**Rationale**: 12 entries are small enough to read at the top of the file. Importable for tests via `from app.services.merchants import KNOWN_MERCHANT_PATTERNS`.

## File Changes

| File | Action | LOC | Description |
|------|--------|-----|-------------|
| `app/models/merchant.py` | Create | ~100 | `Merchant`, `MerchantAlias`, `MerchantAliasSource` enum |
| `app/services/merchants.py` | Create | ~150 | `MerchantNormalizer` + `KNOWN_MERCHANT_PATTERNS` + LLM helper |
| `app/api/v1/merchants.py` | Create | ~80 | `GET /merchants`, `POST /merchants/{id}/aliases` |
| `tests/test_merchants.py` | Create | ~120 | 14 tests (normalization, alias, LLM, API) |
| `app/models/transaction.py` | Modify | ~10 | Add `merchant_id` FK + `merchant_ref` relationship |
| `app/models/__init__.py` | Modify | ~2 | Re-export `Merchant`, `MerchantAlias`, `MerchantAliasSource` |
| `app/core/config.py` | Modify | ~5 | `LLM_MERCHANT_NORMALIZATION_ENABLED: bool = False` |
| `app/schemas/domain.py` | Modify | ~30 | `MerchantResponse`, `MerchantAliasResponse`, `MerchantAliasCreate` |
| `app/schemas/__init__.py` | Modify | ~3 | Re-export new schemas |
| `app/services/ingestion.py` | Modify | ~30 | Additive: alias cache query + per-row resolve + stamp `merchant_id` |
| `alembic/.../0006_..._alter.py` | Modify | ~60 | Extend `upgrade()` + `downgrade()` with PR #4 artifacts |
| `app/api/v1/router.py` | Modify | ~3 | Register `merchants_router` |
| `tests/test_alembic.py` | Modify | ~50 | 2 migration round-trip tests |
| `tests/test_ingestion.py` | Modify | ~30 | 4 merchant-branch tests |

**Total**: ~440 LOC (4 new, 10 modified).

## Testing Strategy

| Layer | Tests | Approach |
|-------|-------|----------|
| Unit | 5 normalization tests | Table-driven: known patterns, `\b` anchor guard (CINEMARK), accent/punctuation strip |
| Unit | 5 alias lookup tests | First-upload creates, second-upload hits, user POST 200/404/422 |
| Unit | 2 LLM helper tests | Flag off = 0 calls, flag on = first-occurrence-only |
| Integration | 4 `_build_transactions` tests | Known pattern stamps `merchant_id`, unknown creates + `low_confidence=True`, LLM flag on/off branches |
| Integration | 2 migration round-trip tests | Assert PR #4 tables + FK + index survive upgrade/downgrade |
| API | 2 GET endpoint tests | Empty list, sorted list |

**Total**: 14 new tests. Coverage target: ≥ 83.17% (PR #2 baseline).

## Migration & Rollback

**No new migration file**. PR #4 extends `0006_phase2_merchants_transactions_alter.py`.

**Upgrade** (appended to existing `upgrade()` body):
1. `CREATE TABLE merchants` (id, name UNIQUE, default_category_id FK→categories SET NULL, is_active, timestamps)
2. `CREATE TABLE merchant_aliases` (id, merchant_id FK→merchants CASCADE, alias_text UNIQUE, normalized INDEXED, source, confidence, timestamps)
3. `ALTER TABLE transactions ADD merchant_id` + FK→merchants SET NULL + index

**Downgrade** (prepended to existing `downgrade()` body — inverse order):
1. Drop `ix_transactions_merchant_id` → `fk_transactions_merchant_id_merchants` → `transactions.merchant_id`
2. `DROP TABLE merchant_aliases`
3. `DROP TABLE merchants`
4. Then existing PR #2 downgrade runs

**Rollback**: revert the commit. `merchant_id` is nullable — existing data intact.

## Cherry-Pick Isolation Gate

| File | Constraint |
|------|-----------|
| `app/services/ingestion.py:483-566` (chunk loop) | **ZERO changes** |
| `app/services/ingestion.py:658-806` (`_build_transactions`) | Additive only (~30 LOC after line 787) |
| `app/services/llm/prompts.py` | **ZERO changes** |
| `app/services/llm/schemas.py` | **ZERO changes** |
| `app/services/llm/*_client.py` | **ZERO changes** |
| `app/web/router.py` | **ZERO changes** |

Apply phase verifies via `git diff main -- app/services/ingestion.py` showing only the additive block.

## Open Questions

None. All 4 design decisions are resolved above. All 3 product decisions are locked (engram #53).
