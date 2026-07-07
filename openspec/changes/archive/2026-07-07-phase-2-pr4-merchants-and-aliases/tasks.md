# Tasks: Merchants + Aliases (Phase 2 PR #4)

## Summary

Total LOC: ~440 | Work units: 8 | PR boundary: single (no chain) | Tests: ~14 new | Coverage target: ≥ 83.17% (PR #2 baseline).

## Review Workload Forecast

| Field | Value |
|-------|-------|
| Estimated changed lines | ~440 |
| 400-line budget risk | Low |
| Chained PRs recommended | No |
| Suggested split | single PR |
| Delivery strategy | force-chained (N/A) |
| Chain strategy | N/A |

Decision needed before apply: No
Chained PRs recommended: No
Chain strategy: N/A
400-line budget risk: Low

## Phase 1: Foundation (models, migration, config)

- [x] **1.1** `feat(models): add Merchant + MerchantAlias + MerchantAliasSource`. Create `app/models/merchant.py` (UUIDMixin+TimestampMixin+Base). `Merchant`: `name` UNIQUE VARCHAR(100), `default_category_id` FK→`categories.id` ON DELETE SET NULL nullable, `is_active=True`, `merchant_transactions` relationship. `MerchantAlias`: `merchant_id` FK→`merchants.id` ON DELETE CASCADE, `alias_text` UNIQUE VARCHAR(200), `normalized` indexed VARCHAR(200), `source='auto'`, `confidence` nullable. `MerchantAliasSource` `str` enum (`'auto'|'user'|'llm'`). Re-export from `app/models/__init__.py`. **Files**: `app/models/merchant.py` (new, ~100 LOC), `app/models/__init__.py` (modified, +2). **Acceptance**: imports clean. **Deps**: none.

- [x] **1.2** `feat(migration): extend 0006 with merchants + merchant_aliases + transactions.merchant_id`. Extend `alembic/versions/0006_*.py` `upgrade()` AFTER PR #2 ALTER: `create_table("merchants")` + FK to `categories.id` SET NULL, `create_table("merchant_aliases")` + FK CASCADE + UNIQUE(`alias_text`) + index on `normalized`, `batch_op.add_column("merchant_id")` + FK SET NULL + index. `downgrade()` drops PR #4 artifacts first (inverse order). Update docstring. Add 2 round-trip tests in `tests/test_alembic.py`. **Files**: `alembic/versions/0006_*.py` (modified, +60), `tests/test_alembic.py` (modified, +50). **Acceptance**: `upgrade head` produces 4 tables + 3 FKs; `downgrade -1` reverses. **Deps**: none (schema-only).

- [x] **1.3** `feat(models): add merchant_id FK to Transaction`. In `app/models/transaction.py`, add `merchant_id: Mapped[uuid.UUID | None]` with FK→`merchants.id` ON DELETE SET NULL + index after `category_id`; add `merchant_ref: Mapped["Merchant | None"]` relationship after `category_ref`. **Files**: `app/models/transaction.py` (modified, +10). **Acceptance**: model imports; migration round-trip works. **Deps**: 1.1.

- [x] **1.4** `feat(config): add LLM_MERCHANT_NORMALIZATION_ENABLED setting`. In `app/core/config.py`, add `LLM_MERCHANT_NORMALIZATION_ENABLED: bool = Field(default=False, description=...)` following the `LLM_*` pattern. **Files**: `app/core/config.py` (modified, +5). **Acceptance**: `settings.LLM_MERCHANT_NORMALIZATION_ENABLED == False`. **Deps**: none.

## Phase 2: Core Service

- [x] **2.1** `feat(services): add MerchantNormalizer + KNOWN_MERCHANT_PATTERNS`. Create `app/services/merchants.py` (per D4, module-level). `KNOWN_MERCHANT_PATTERNS: dict[str, str]` — 12 Chilean merchants → `Category.name` (MCDONALDS/STARBUCKS→Dining Out, LIDER→Groceries, PARIS/SODIMAC/EASY/AMAZON→Shopping, COPEC/SHELL/UBER→Transportation, NETFLIX/SPOTIFY→Subscriptions; per D1). `normalize(raw) -> str`: lowercase → NFKD accent strip → single `re.sub` with `\b`-anchored `S\.A\.|LTDA|CIA|SUCURSAL|\bSUC\b|\bCOM\b` → strip digits → strip `/;.,` → collapse whitespace. `resolve_merchant(session, description, categories_by_name) -> tuple[Merchant, bool]`: lookup by `normalized`; on miss check `KNOWN_MERCHANT_PATTERNS` for default category, create `Merchant` + `MerchantAlias(source='auto')`; try/except `IntegrityError` race guard (D3); `low_confidence=True` on miss (per D9). `resolve_merchant_with_llm(session, description, llm_provider) -> Merchant`: opt-in (per D6), first-occurrence-only, uses existing `LLMProvider` protocol, caches with `source='llm'` + `confidence=<score>`. Add 12 tests in `tests/test_merchants.py` (5 normalization incl. `\b` CINEMARK vs CIA + 5 alias hit/miss/user-POST + 2 LLM flag on/off). **Files**: `app/services/merchants.py` (new, ~150), `tests/test_merchants.py` (new, +120). **Acceptance**: 12 known patterns resolve; `\b` anchor guard holds; race guard catches `IntegrityError`; LLM helper no-op when flag off. **Deps**: 1.1.

## Phase 3: Integration (API + ingestion)

- [x] **3.1** `feat(api): add GET /merchants + POST /merchants/{id}/aliases`. Create `app/api/v1/merchants.py`. `list_merchants(session) -> list[MerchantResponse]`: `select(Merchant).order_by(name.asc())`. `add_alias(merchant_id, payload, session) -> MerchantAliasResponse`: 404 if missing, 422 on `UNIQUE(alias_text)` collision, atomic single `commit()` with `source='user'` + `normalized=normalize(payload.alias_text)`. Add `MerchantResponse`, `MerchantAliasResponse`, `MerchantAliasCreate` (alias_text min=1, max=200) to `app/schemas/domain.py`; re-export. Register router in `app/api/v1/router.py` between `categories_router` and `statements_router`. Add 4 API tests (empty list, sorted list, POST 200/404/422). **Files**: `app/api/v1/merchants.py` (new, ~80), `app/schemas/domain.py` (+30), `app/schemas/__init__.py` (+3), `app/api/v1/router.py` (+3), `tests/test_merchants.py` (+30). **Acceptance**: both endpoints respond per spec R5/R6; router registered. **Deps**: 1.1.

- [x] **3.2** `feat(ingestion): integrate merchant normalization into _build_transactions`. In `app/services/ingestion.py:_build_transactions` after category resolution (~line 787), add: (a) one-query dict cache `merchant_aliases_by_normalized: {ma.normalized: ma for ma in ...}` mirroring categories pattern; (b) per-row: `normalized = normalize(txn.description)`; hit → `merchant_id = alias.merchant_id`; miss → `merchant, was_new = await MerchantNormalizer.resolve_merchant(self._session, txn.description, categories_by_name)`; flip `low_confidence = low_confidence or was_new` (D2 OR semantics); (c) stamp `merchant_id` on inserted `Transaction`. Chunk loop (lines 483-566), `try/finally`, `first_successful_chunk_seen`, `last_chunk_exc`, all-fail guard, metadata-None guard, counters, `_metadata_completeness` are UNTOUCHED. Add 4 tests in `tests/test_ingestion.py` (`_known_pattern_stamps_merchant_id`, `_unknown_pattern_creates_low_confidence`, `_llm_helper_flag_off`, `_llm_helper_flag_on`). **Files**: `app/services/ingestion.py` (modified, +30), `tests/test_ingestion.py` (modified, +30). **Acceptance**: `git diff main -- app/services/ingestion.py` shows additive only; new tests pass; existing category tests still pass. **Deps**: 2.1.

## Phase 4: Test Consolidation

- [x] **4.1** `test(merchants): consolidate coverage + edge cases`. Verify all new tests from 1.2, 2.1, 3.1, 3.2 are present, well-named, run in the suite. Add any missing edge cases (e.g., `\b` anchor for `CINEMARK` vs `CIA`, race guard, LLM opt-in, normalized-text reuse). **Files**: `tests/test_merchants.py` (consolidated, ~150 LOC). **Acceptance**: test suite runs cleanly; coverage shows new paths exercised; `ruff check` + `mypy --strict` clean. **Deps**: 1.2, 2.1, 3.1, 3.2.

## Cherry-Pick Isolation Gate (apply verifies)

- `app/services/ingestion.py:483-566` (chunk loop): **ZERO changes** ✓
- `app/services/llm/{prompts,schemas,*_client}.py`: **ZERO changes** ✓
- `app/web/router.py`: **ZERO changes** ✓
- `_build_transactions`: additive only (~30 LOC after line 787) ✓
- Verify via `git diff main -- app/services/ingestion.py`

## Constraints

- NO new migration file (extend 0006). NO new column on `categories` (FK only).
- NO change to LLM prompt, clients, or schemas. NO change to chunk loop / `try/finally` / counters / `_metadata_completeness`.
- DO NOT add `Co-Authored-By` to commits. DO NOT skip tests or lint/type gates.
