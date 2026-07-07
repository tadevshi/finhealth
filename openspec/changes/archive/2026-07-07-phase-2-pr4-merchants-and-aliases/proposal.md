# Proposal: Merchants + Aliases (Phase 2 PR #4)

## Intent

Today every transaction carries a free-text `description` from the bank — there is no canonical merchant entity. The user cannot answer "what did I spend at Lider?" without scanning every row manually. PR #4 introduces `merchants` and `merchant_aliases` tables, a deterministic normalizer that auto-creates canonical merchants from bank descriptions (~80% coverage, zero LLM cost), and an opt-in LLM helper for ambiguous cases.

## Scope

### In Scope
- `merchants` + `merchant_aliases` tables, `Transaction.merchant_id` FK (extends migration 0006)
- `MerchantNormalizer` service (regex-based normalization with `\b` anchors, alias-table lookup, auto-create on miss) — architecture picks A/B
- Opt-in LLM helper (`LLM_MERCHANT_NORMALIZATION_ENABLED`, default `false`) — architecture pick C
- Hardcoded `KNOWN_MERCHANT_PATTERNS` dict (~10-15 Chilean merchants → default category) — architecture pick D
- `GET /api/v1/merchants` + `POST /api/v1/merchants/{id}/aliases` endpoints
- ~14 new tests (normalization table-driven, alias hit/miss/auto-create, LLM flag on/off, API 200/404/422, migration round-trip, ingestion branches)
- `app/services/ingestion.py:_build_transactions` — merchant resolution (one query + dict cache, per-row lookup). Chunk loop, `try/finally`, counters, `_metadata_completeness` are untouched.

### Out of Scope
- Bulk merchant assignment UI (anti-feature per decision #4)
- LLM prompt change (the LLM continues to emit the raw `description`; normalization is server-side)
- `KNOWN_MERCHANT_PATTERNS` admin endpoint (hardcoded dict in v1)
- Phase 2 PRs #5-#6 (Recurring detection, Docs + e2e)
- Renaming the seeded `categories` table
- The cherry-pick chunk-loop guard (untouched, already in main)

## Capabilities

### New Capabilities
- `phase2-merchant-aliasing`: merchant canonicalization, alias management, deterministic + LLM normalization, merchant API endpoints. (This was a stub in the PR #2 archive; PR #4 elaborates the full spec.)

### Modified Capabilities
- `phase2-categories`: migration 0006 is extended with two additional tables and one FK. The main spec requirements are unchanged; this is a structural addition to an existing capability.

## Approach

**Architecture picks** (from explore #102, user-approved):
- **A**: Regex with `\b` anchors — one-pass `re.sub` for legal-entity tokens, digits, accents, punctuation.
- **B**: Hybrid v2 alias lookup — `UNIQUE(alias_text)` raw + non-unique `normalized` column indexed.
- **C**: First-occurrence-only LLM trigger — one LLM call per unique normalized text per ingestion.
- **D**: Hardcoded `KNOWN_MERCHANT_PATTERNS` dict in `app/services/merchants.py` for v1.

**Product decisions** (engram #53, locked): #4 (auto-create + alias for known patterns), #6 (LLM flag off by default), #9 (NULL `default_category_id` + `low_confidence=True` for new merchants).

**File changes**: 4 new files (models, service, API router, tests), 10 modified files (transaction model, config, schemas, ingestion, migration 0006, router, model/schema `__init__`, 2 test files).

## Affected Areas

| Area | Impact | Description |
|------|--------|-------------|
| `app/models/merchant.py` | New | `Merchant` + `MerchantAlias` + `MerchantAliasSource` enum |
| `app/services/merchants.py` | New | `MerchantNormalizer` + `KNOWN_MERCHANT_PATTERNS` + LLM helper |
| `app/api/v1/merchants.py` | New | `GET /api/v1/merchants`, `POST /api/v1/merchants/{id}/aliases` |
| `app/models/transaction.py` | Modified | Add `merchant_id` FK + index |
| `app/services/ingestion.py` | Modified | ~30 LOC in `_build_transactions` (query + dict cache + per-row lookup) |
| `alembic/.../0006_..._alter.py` | Modified | Extend upgrade/downgrade with merchants + aliases + FK |
| `app/core/config.py` | Modified | `LLM_MERCHANT_NORMALIZATION_ENABLED: bool = False` |
| `app/schemas/domain.py` | Modified | `MerchantResponse`, `MerchantAliasResponse`, `MerchantAliasCreate` |

## Risks

| Risk | Likelihood | Mitigation |
|------|------------|------------|
| Regex over/under-normalization (e.g., `CIA` stripping `CINEMARK`) | Medium | Table-driven test suite with Chilean bank strings + negative case |
| Alias text collision on user POST | Low | `UNIQUE(alias_text)` constraint + 422 with clear message |
| LLM helper blow-up on opt-in (30 unique merchants = 30 LLM calls) | Low | First-occurrence-only trigger; flag off by default; test asserts max 1 call per unique normalized text |
| Migration 0006 size (~120 lines upgrade body) | Low | Docstring labels each PR section; round-trip test asserts both PR #2 and PR #4 artifacts |
| Cherry-pick isolation leak (chunk-loop code touched) | Low | Gate enforced in `test_ingestion.py` + apply-phase diff review |

## Rollback Plan

Revert the commit. `transactions.merchant_id` is nullable — existing data is intact. The downgrade drops PR #4 artifacts first (index, FK, column, `merchant_aliases` table, `merchants` table), then PR #2 artifacts. The normalization service and API router are removed.

## Dependencies

- **PR #2 (Categories Foundation)** — in main, provides `Category` model, seeded taxonomy, `low_confidence` Boolean, migration 0006 extension point.
- **PR #3 (Categories UI)** — in main, provides per-row `<select>` pattern reused conceptually for merchant assignment.
- **Cherry-pick (`ingestion-tolerate-partial-chunk-failures`)** — in main, provides `_build_transactions` async structure + `low_confidence` pattern.

## Success Criteria

- [ ] Migration 0006 extension lands: `merchants` + `merchant_aliases` tables, `transactions.merchant_id` FK + index
- [ ] 14 new tests pass (coverage ≥ 83.17%, PR #2 baseline)
- [ ] Cherry-pick isolation holds — zero changes to chunk loop, `try/finally`, counters, `_metadata_completeness`
- [ ] `ruff check` + `mypy --strict` clean
- [ ] Ingestion resolves known patterns (MCDONALDS → Dining Out) and auto-creates unknown variants with `low_confidence=True`
