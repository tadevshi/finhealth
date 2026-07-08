# Archive Report: phase-2-pr5-recurring-detection

**Change**: `phase-2-pr5-recurring-detection`
**Work unit**: PR #5 — Recurring Detection
**Archived on**: 2026-07-08
**Status**: MERGED + ARCHIVED (SDD cycle complete for PR #5)

## Summary

PR #5 introduces the `phase2-recurring-detection` capability — a deterministic recurring-transaction detector that runs at the end of every successful `ingest_statement`, plus two endpoints (`GET /api/v1/recurring`, `PATCH /api/v1/recurring/{id}`) for the user to review and override detected rules. The detector groups transactions on the same `credit_card_id` from the last 90 days by `(merchant_id, currency)`, requires ≥3 occurrences within ±15% of the median amount, classifies the cadence by the median interval, and upserts a `RecurringRule` keyed on `(merchant_id, amount_min, amount_max, currency, period_days)`. PR #5 is the 4th of the 5-PR chained Phase 2 plan (#2-#6); PR #6 (Docs + e2e) will follow as its own SDD cycle.

## Merge State

- **PR**: https://github.com/tadevshi/finhealth/pull/33
- **Status**: MERGED
- **Merged at**: 2026-07-08
- **Merge commit**: `647229c159db9d827b66dd1ce8a6bc2db2c2b372`
- **Base branch**: `main` (parent: `d44c204` — PR #4 archive commit)
- **Feature branch**: `feat/phase2-pr5-recurring-detection` (merged + deleted, both local and remote)
- **Worktree**: `/tmp/opencode/finhealth-pr33` (removed after merge)

## Commit Trail (18 total on the branch)

### 8 original commits (PR-author)

| # | SHA | Subject |
|---|-----|---------|
| 1 | `4f0ee42` | `feat(models): add RecurringRule model + merchant relationship` |
| 2 | `423d72f` | `feat(migration): add 0007_phase2_recurring_rules + transactions.recurring_rule_id FK` |
| 3 | `2fdb1b6` | `feat(schemas): add RecurringRuleResponse + RecurringRuleUpdate Pydantic models` |
| 4 | `074d47b` | `feat(recurring): add RecurringDetector service + GET/PATCH /api/v1/recurring endpoints` |
| 5 | `131e1f3` | `feat(ingestion): wire RecurringDetector into ingest_statement (6 LOC additive)` |
| 6 | `4469af9` | `test(ingestion): add 4 recurring-detector integration tests` |
| 7 | `1563984` | `style: apply ruff format to tests/test_recurring.py` |
| 8 | `1fcb2c5` | `chore(sdd): add apply-progress + mark all 7 tasks complete for PR #5` |

### 10 judgment-day fixes (post-review)

| # | SHA | Subject |
|---|-----|---------|
| 9 | `eeab82b` | `fix(recurring): remove duplicate _classify_period call` |
| 10 | `d0d5f5a` | `fix(recurring): skip rule for same-day occurrences` |
| 11 | `3749c8a` | `perf(model): change RecurringRule relationships to lazy=noload` |
| 12 | `57c9a6b` | `docs(spec): reconcile spec scenarios with implementation` |
| 13 | `72c4388` | `test(recurring): add 6 spec coverage + 4th boundary row` |
| 14 | `61d7174` | `docs(sdd): write verify-report.md for PR #5` |
| 15 | `f8b3bb1` | `fix(recurring): tighten same-day guard and update stale relationship docstring` |
| 16 | `706dd5e` | `docs(spec): reconcile outlier worked example with full-group median; tighten test` |
| 17 | `afcd8ec` | `fix(model): correct noload opt-in recipe — use selectinload, not session.refresh` |
| 18 | `4594073` | `test+docs(recurring): add majority-same-day regression test; fix verify-report` |

No `Co-Authored-By` trailers. No `Generated with...` footers. Conventional commits only.

## Implementation State

### Production code (4 new files, 8 modified)

**New files**:
- `app/models/recurring_rule.py` — `RecurringRule(UUIDMixin, TimestampMixin, Base)` model with `merchant_id` FK (CASCADE), `period_days`, `period_label` (weekly/biweekly/monthly/quarterly/yearly), `amount_min`/`max`, `currency`, `is_active`, `confidence`, `last_seen_date`, `occurrences`; relationships use `lazy="noload"` (opt-in eager load required; commit `3749c8a` + `afcd8ec`)
- `alembic/versions/0007_phase2_recurring_rules.py` — `recurring_rules` table + composite index `(merchant_id, currency, period_days)` + `ix_recurring_rules_is_active`; `batch_alter_table("transactions")` adds `recurring_rule_id` column + FK→`recurring_rules.id` ON DELETE SET NULL + index. `down_revision = "0006_phase2_merchants_transactions_alter"`
- `app/services/recurring_detection.py` — `class RecurringDetector(session, partial_success)`. `async def detect(statement)`. One-query 90-day scan, group by `(merchant_id, currency)`, ≥3 occurrences, ±15% band on full-group median, period classification, idempotent upsert, confidence = `min(1.0, occurrences/5) * max(0.0, 1.0 - (max-min)/median)` (Python-side, 4-decimal rounding), FK backfill
- `app/api/v1/recurring.py` — `GET /api/v1/recurring` (filter `is_active=True`, order `last_seen_date` desc) + `PATCH /api/v1/recurring/{id}` (`is_active` override; 404 on unknown id; FK on historical transactions preserved)

**Modified files**:
- `app/models/__init__.py` — re-export `RecurringRule`
- `app/models/transaction.py` — added `recurring_rule_id` FK (SET NULL) + `recurring_rule_ref` relationship
- `app/schemas/domain.py` — `RecurringRuleResponse` (with `confidence: float = Field(..., ge=0.0, le=1.0)`) + `RecurringRuleUpdate(is_active: bool)` with `extra="forbid"`
- `app/schemas/__init__.py` — re-export new schemas
- `app/api/v1/router.py` — register `recurring_router` between `merchants_router` and `statements_router`
- `app/services/ingestion.py` — **4 additive source-LOC** (import + `__init__` field + 1-line stash + `try/except` detector call wrapper) plus comments/docstrings
- `tests/test_alembic.py` — +2 round-trip tests for the new table + FK
- `tests/test_recurring.py` (new) — 18 tests: 8 algorithm + 4 API + 2 logging + 4 ingestion-integration
- `tests/test_ingestion.py` — +4 recurring-detector integration tests (gated by `@needs_sample_pdfs` + `@needs_test_rut`)

## Test count delta

- **Pre-PR-#5 baseline**: 385 passing tests (with 16 pre-existing Zen failures in `tests/test_llm_services.py` and 69 skipped)
- **Post-PR-#5**: 405 passing tests (385 + 20 new tests), 73 skipped, 16 pre-existing Zen failures
- **Net new tests**: +20
- **No regressions**

## Coverage

| Suite | Coverage | Note |
|-------|----------|------|
| Pre-PR-#5 baseline (full test suite) | 83.56% | `app` package, PR #4 main |
| Post-PR-#5 (full test suite, 16 Zen failures included) | **84.31%** | Above baseline ✓ |

Per-file coverage on the new code:
- `app/models/recurring_rule.py` — 100% (24/24 statements)
- `app/services/recurring_detection.py` — 95.97% (3 missed lines: defensive `median_amount == 0` guard + a single never-hit branch in the per-group filter)
- `app/api/v1/recurring.py` — 70.37% (FastAPI ASGITransport instrumentation does not track endpoint function calls in this environment; same pattern as `app/api/v1/merchants.py` at 56.41% and `app/api/v1/categories.py` at 57.78%)

## Cherry-Pick Isolation Gate: PASS

`git diff main..feat/phase2-pr5-recurring-detection -- app/services/ingestion.py` showed ONLY 4 additive source-LOC changes:

1. **Import** (line 73): `from app.services.recurring_detection import RecurringDetector`
2. **`__init__` field** (line 176): `self._last_failed_chunks = 0` (defensive init)
3. **Detector call wrapper in `ingest_statement`** (lines 437-456): the `try/except` block that calls `RecurringDetector.detect`
4. **1-line stash in `_run_chunked_extraction`** (line 626): `self._last_failed_chunks = failed_chunks` — **KNOWN RISK #1** (additive only, no flow change)

The diff to protected files is ZERO:

| File | Diff |
|------|------|
| `app/services/llm/prompts.py` | 0 lines |
| `app/services/llm/schemas.py` | 0 lines |
| `app/services/llm/ollama_client.py` | 0 lines |
| `app/services/llm/opencode_zen_client.py` | 0 lines |
| `app/services/llm/opencode_go_client.py` | 0 lines |
| `app/services/merchants.py` | 0 lines |
| `app/web/router.py` | 0 lines |
| `app/core/config.py` | 0 lines |

The chunk loop, `try/finally`, `first_successful_chunk_seen` flag, `last_chunk_exc` chaining, all-fail guard, metadata-None guard, counters, and `_metadata_completeness` are all UNTOUCHED.

## Judgment-Day History: 3 rounds, 10 fix commits

| Round | Date | Findings | Outcome |
|-------|------|----------|---------|
| Round 1 | 2026-07-07 | 1 CONFIRMED + 11 SUSPECT | 10 fix commits landed |
| Round 2 | 2026-07-08 | 2 spec reconciliations | 2 spec edits (outlier worked example + grouping key) |
| Round 3 | 2026-07-08 | FINAL APPROVED | All real issues resolved |

### CONFIRMED (1)

1. **Two-cards scenario — model mismatch.** The spec grouped by `(credit_card_id, merchant_id, currency)` and required two rules for the same merchant+currency on different cards. The implementation groups by `(merchant_id, currency)` only — the `RecurringRule` model has no `credit_card_id` column. **Resolution**: spec downgraded to `(merchant_id, currency)`; the two-cards scenario was removed (out of scope — adding `credit_card_id` requires a schema migration). The detector scans per-`credit_card_id` (a filter, not a grouping key), so within a single `detect()` call all rows are on the same card.

### SUSPECT resolved as real issues (3)

1. **Dead code** — `app/services/recurring_detection.py` had a duplicate call to `self._classify_period(median_interval)` on line 360 (same call as line 359). Removed in `eeab82b`.
2. **Same-day false-positive** — three same-day transactions previously produced a meaningless rule with `period_label="weekly"` and `period_days=0`. The original guard was `if all(i == 0 for i in intervals): return None` (only caught the all-zero case). Strengthened in `d0d5f5a` to `if median_interval == 0: return None` AFTER computing the median, which catches both the all-zero case AND the majority-same-day case. Further tightened in `f8b3bb1` and locked with `test_majority_same_day_with_one_later_does_not_create_rule` in `4594073`.
3. **Eager-loading waste** — `RecurringRule.merchant` and `RecurringRule.transactions` were `lazy="joined"` and `lazy="selectin"`. Both fields are not in the API response, so the default read paid for a JOIN and a second round-trip. Both changed to `lazy="noload"` in `3749c8a`. The opt-in recipe in the docstring was corrected in `afcd8ec` (use `selectinload`, not `session.refresh` — `session.refresh` does NOT override `noload` on SQLAlchemy 2.0).

### SUSPECT left as INFO (2, out of scope)

1. **NaN/inf defense in confidence arithmetic** — informational only; `median_amount` is a `Decimal` from in-band transactions, and the ±15% band is computed via `Decimal` arithmetic. No path in the v1 detector produces NaN/inf on the in-band subset. Out of scope for judgment-day fixes.
2. **Theoretical race condition in the upsert** — informational only; the detector runs sync inside the ingest request, so two concurrent ingests on the same card would share the session. Current design accepts this; the upsert is idempotent so worst case is a slightly stale `last_seen_date` on one of the two writes. Out of scope for judgment-day fixes.

### Spec reconciliations (2, in commit 57c9a6b + 706dd5e)

1. **Outlier scenario — 3 → 4 occurrences.** The original spec gave a 3-occurrence example (`$10.00, $10.50, $100.00`) and claimed the in-band median was `$10.25` with 3 in-band rows. The arithmetic did not check out: `statistics.median([10.00, 10.50, 100.00])` is `10.50` (not `10.25`), the ±15% band on `10.50` is `[8.925, 12.075]`, only `[10.00, 10.50]` is in-band (2 rows — below the 3-occurrence threshold), and the spec's `0.6 * 0.951` factor assumed 3 in-band rows. The spec was updated to a 4-occurrence example (`$10.00, $10.25, $10.50, $100.00`) where the math is consistent: full-group median `10.375`, band `[8.82, 11.93]`, 3 in-band rows spanning `$10.00-$10.50`, and `confidence = 0.6 * (1.0 - 0.50/10.375) = 0.6 * 0.9518 = 0.5711` (the formula uses the **full-group** median, not the in-band subset median; the test locks this with a `0.0001` tolerance that would fail at `0.5707`).
2. **Grouping header downgraded** from `(credit_card_id, merchant_id, currency)` to `(merchant_id, currency)` (same root cause as the two-cards scenario above).

## Specs Synced

| Domain | Action | Details |
|--------|--------|---------|
| `phase2-recurring-detection` | Created | Full new main spec from delta. All 7 `ADDED Requirements` copied verbatim to `openspec/specs/phase2-recurring-detection/spec.md` (208 lines). |

The delta spec is a full new capability (the `phase2-recurring-detection` domain did not exist in `openspec/specs/`), so the entire spec was copied as-is — no requirement-name merging required.

## Source of Truth Updated

The following spec now reflects the new behavior:
- `openspec/specs/phase2-recurring-detection/spec.md` (new file, 208 lines)

The `phase2-recurring-detection` capability is the 4th Phase 2 capability registered in the spec tree, after:
- `openspec/specs/ingestion-chunk-failure-tolerance/spec.md` (PR #1, archived 2026-06-29)
- `openspec/specs/phase2-categories/spec.md` (PR #2, archived 2026-06-29)
- `openspec/specs/phase2-merchant-aliasing/spec.md` (PR #4, archived 2026-06-29)

PR #3 (Categories UI) is not a new spec — it elaborates `phase2-categories` and stays in the active change folder on `feat/phase2-pr3-categories-ui` until archived.

## Risks Carried Forward

### KNOWN RISK #1 (documented in the design)

The 1-line stash `self._last_failed_chunks = failed_chunks` in `_run_chunked_extraction` is inside the cherry-pick-protected function. The stash is **additive** (no flow change — the loop, `try/finally`, all-fail guard, metadata-None guard, counters, and `_metadata_completeness` are untouched). The PR body called this out for the reviewer per the design's explicit instruction.

### `lazy="noload"` opt-in pattern

Any future code that touches `rule.merchant` or `rule.transactions` without an explicit eager load (e.g. `selectinload(RecurringRule.transactions)` in a query) will trigger a lazy load (which may be surprising). The model docstring documents the opt-in pattern. Commits `3749c8a` and `afcd8ec` established this convention.

### Two-cards behavior (out of scope)

The two-cards scenario is documented as out of scope. A future PR may add `credit_card_id` to the `RecurringRule` model and re-introduce the scenario.

## Gate Results

| Gate | Result |
|------|--------|
| `pytest tests/ -q` (full suite) | 16 failed (pre-existing Zen), 405 passed, 73 skipped |
| `pytest tests/ -q --ignore=tests/test_llm_services.py` | 333 passed, 73 skipped, 0 failures |
| `pytest tests/test_recurring.py -v` | 18 passed |
| `pytest tests/test_alembic.py -v` | 16 passed (including 2 new round-trip tests) |
| `pytest tests/test_merchants.py -v` | 44 passed (no regression) |
| `pytest tests/test_categories.py -v` | included in 44 |
| `ruff check .` | All checks passed |
| `ruff format --check .` | 8 pre-existing format failures; new files are clean |
| `mypy --strict app/` | 1 pre-existing error in `app/services/llm/opencode_zen_client.py:338`; clean on the new modules |
| Cherry-pick isolation audit | **PASS** — only 4 additive source-LOC in `ingestion.py` |
| Coverage ≥ 83.17% (PR #2 baseline) | **84.31%** with full test suite |
| Verify report status | **PASS** |
| Judgment-day rounds | **3 rounds, FINAL APPROVED** |

## Archive Contents

- `proposal.md` (6,085 bytes)
- `specs/phase2-recurring-detection/spec.md` (208 lines)
- `design.md` (6,380 bytes)
- `tasks.md` (11,317 bytes — all 7 tasks marked `[x]`)
- `apply-progress.md` (11,542 bytes — all 7 tasks complete)
- `verify-report.md` (6,447 bytes — status: PASS)
- `archive-report.md` (this file)

7/7 tasks complete. Source of truth updated. SDD cycle complete for PR #5. Ready for PR #6 (Docs + e2e) as the final work unit of the 5-PR Phase 2 plan.

## Next Step

- PR #6 (Phase 2 — Docs + e2e) is the only remaining work unit. Open a new SDD cycle for it when ready.
