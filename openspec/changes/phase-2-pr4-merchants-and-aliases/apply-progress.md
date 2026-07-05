# Apply Progress: Merchants + Aliases (Phase 2 PR #4)

## Status: complete — ready for verify

## Branch

- `feat/phase2-pr4-merchants-and-aliases` branched from `main` at `0cf5a6e` (post-PR #3 archive commit).
- 5 atomic conventional commits, no `Co-Authored-By`, no `Generated with...` footers.

## Commits

| SHA | Type | Scope | Subject |
|-----|------|-------|---------|
| `f22961a` | feat | phase2 | add Merchant + MerchantAlias models, transactions.merchant_id FK, LLM_MERCHANT_NORMALIZATION_ENABLED config |
| `0b81dd0` | feat | services | add MerchantNormalizer + KNOWN_MERCHANT_PATTERNS + opt-in LLM helper |
| `30b928b` | feat | api | add GET /merchants + POST /merchants/{id}/aliases + Pydantic schemas |
| `cff00cd` | feat | ingestion | integrate merchant normalization into _build_transactions |
| `2944d01` | test | merchants | consolidate coverage with normalize + alias + LLM + API + alembic round-trip |

## Test delta

- Pre-PR #4 baseline: **349 passing** + 16 pre-existing Zen LLM failures + 69 skipped.
- Post-PR #4: **379 passing** + 16 pre-existing Zen LLM failures + 69 skipped.
- Net new tests: **30** (28 in `tests/test_merchants.py` + 2 in `tests/test_alembic.py`).
- The 4 spec-targeted merchant integration tests in `tests/test_ingestion.py` (TestBuildTransactionsMerchantResolution) and 2 existing PR #2 tests updated to use known-pattern descriptions.

## Coverage delta

- Pre-PR #4 baseline: 83.67% (per the prompt, the PR #2 baseline floor is 83.17%).
- Post-PR #4: **83.44%** (243 uncovered statements across 1,824 total).
- 0.23pp drop, all in the new code paths (`app/services/merchants.py` and `app/api/v1/merchants.py`). Above the 83.17% floor.

## Files changed

### New files (4)

| File | LOC | Description |
|------|-----|-------------|
| `app/models/merchant.py` | 204 | `Merchant`, `MerchantAlias`, `MerchantAliasSource` enum. UUIDMixin+TimestampMixin+Base. |
| `app/services/merchants.py` | 632 | `KNOWN_MERCHANT_PATTERNS`, `normalize()`, `MerchantNormalizer` (resolve_merchant + resolve_merchant_with_llm), `_extract_canonical_from_llm`. |
| `app/api/v1/merchants.py` | 193 | `GET /api/v1/merchants` + `POST /api/v1/merchants/{id}/aliases`. |
| `tests/test_merchants.py` | 673 | 28 tests (normalize, alias lookup, LLM helper, API, KNOWN_MERCHANT_PATTERNS contract). |

### Modified files (10)

| File | +/- | Description |
|------|-----|-------------|
| `app/models/__init__.py` | +4 | Re-export `Merchant`, `MerchantAlias`, `MerchantAliasSource`. |
| `app/models/transaction.py` | +28/-0 | Add `merchant_id` FK + `merchant_ref` relationship. |
| `app/core/config.py` | +23/-0 | Add `LLM_MERCHANT_NORMALIZATION_ENABLED: bool = False`. |
| `app/schemas/__init__.py` | +6/-0 | Re-export `MerchantResponse`, `MerchantAliasResponse`, `MerchantAliasCreate`. |
| `app/schemas/domain.py` | +74/-0 | Add `MerchantResponse`, `MerchantAliasResponse`, `MerchantAliasCreate`. |
| `app/api/v1/router.py` | +4/-0 | Register `merchants_router`. |
| `app/services/ingestion.py` | +79/-0 | Additive: alias cache + per-row merchant resolution + stamp `merchant_id`. |
| `alembic/versions/0006_phase2_merchants_transactions_alter.py` | +182/-50 | Extend `upgrade()` + `downgrade()` with PR #4 artifacts. |
| `tests/test_ingestion.py` | +6/-3 | Update 2 PR #2 tests (LEADER → LIDER, X/Y → MCDONALDS/STARBUCKS). Add 4 new `TestBuildTransactionsMerchantResolution` tests. |
| `tests/test_alembic.py` | +172/-0 | Add 2 migration round-trip tests. |

## Cherry-pick isolation audit (gate)

`git diff main -- app/services/ingestion.py`:

```text
... +79 lines, 0 deletions
... all additions are after line 734 (after the categories cache
...   in _build_transactions)
... no changes to the chunk loop (lines 483-566)
... no changes to _run_chunked_extraction, _metadata_completeness,
...   _validate_metadata, _build_transactions signature
```

**Result**: PASS. The diff is purely additive.

- `app/services/llm/prompts.py`: **ZERO changes** ✓
- `app/services/llm/schemas.py`: **ZERO changes** ✓
- `app/services/llm/{ollama,opencode_zen,opencode_go}_client.py`: **ZERO changes** ✓
- `app/services/llm/protocol.py`: **ZERO changes** ✓
- `app/services/llm/factory.py`: **ZERO changes** ✓
- `app/web/router.py`: **ZERO changes** ✓

## Gates

| Gate | Result |
|------|--------|
| `pytest tests/ -q` | 379 passed, 16 pre-existing Zen failures, 69 skipped |
| `pytest tests/ -q --cov=app --cov-report=term` | 83.44% (above 83.17% floor) |
| `pytest tests/test_merchants.py -v` | 28 passed |
| `pytest tests/test_ingestion.py -v` | 26 passed (including 4 new + 2 updated) |
| `pytest tests/test_alembic.py -v` | 14 passed (including 2 new) |
| `ruff check .` | All checks passed (only pre-existing files unformatted, none of mine) |
| `ruff format --check` (modified files) | 13 files already formatted (my 4 new + 9 modified) |
| `mypy --strict app/` | 1 pre-existing error in `opencode_zen_client.py:338` (NOT a regression) |
| `mypy --strict` (my 4 new files) | Success: no issues found |
| Migration `upgrade head` / `downgrade base` round-trip | Works on `:memory:` + file SQLite |

## Deviations from design

1. **Normalization punctuation strip**: The design says "strip punctuation `/;.,`" but the spec scenarios require `"S.A. PARIS 03/06"` → `"paris 03/06"` (installment preserved). The implementation uses a placeholder protect pass to capture `NN/NN` patterns *before* the punctuation strip, then restores them. The end result matches every spec scenario in the proposal.
2. **`S.A.` lookbehind/lookahead**: The design's `re.sub` with `\b`-anchored alternation for `S.A.` does not work because the trailing period sits between two non-word characters and therefore has no `\b` word boundary after it. The implementation uses `(?<!\w)S\.A\.(?!\w)` instead, which covers the same ground without the false negative.
3. **Existing PR #2 test descriptions**: Two `TestBuildTransactions` tests in `tests/test_ingestion.py` used descriptions (`"LEADER"`, `"X"`, `"Y"`) that do not match `KNOWN_MERCHANT_PATTERNS` after PR #4. Per design decision D2, an auto-created merchant whose canonical is not in `KNOWN_MERCHANT_PATTERNS` flips `low_confidence=True` — so the existing PR #2 assertions of `low_confidence=False` would fail. The two tests' descriptions were updated to use known-pattern merchants (`"LIDER"`, `"MCDONALDS"`, `"STARBUCKS"`) so the existing closed-set category behaviour is still tested and the new PR #4 path is exercised end-to-end.
4. **LLM helper merchant-table lookup**: The design's `resolve_merchant_with_llm` always creates a new merchant, but the `UNIQUE(merchants.name)` constraint would reject a second LLM call that returns the same canonical name. The implementation adds a merchant-table hit-or-create lookup after the LLM call: a hit binds the new alias to the existing merchant (`was_new=False`), a miss creates a new one. This matches the `alias_text` UNIQUE guard's pattern (D3) and avoids the `IntegrityError` race-recovery path on `merchants.name`.

## Issues found

None — the implementation matches the design, the spec scenarios all pass, and the cherry-pick isolation holds.

## Next phase

`sdd-verify` for PR #4 — run the full test suite + the migration round-trip + the cherry-pick isolation gate, produce the verify-report.md.
