# Proposal: Phase 2 — Classification

## Intent

finhealth Phase 1 extracts every line-item from bank PDFs, but `Transaction.category` is free-text (unvalidated string in LLM output, no taxonomy, no enforcement). The user must manually assign categories to every row. Phase 2 introduces a controlled 12-category Y-NAB-style taxonomy enforced at ingestion time, a merchant canonicalization layer so "LA POLAR SUC. 123" and "LA POLAR" become the same store, and recurring-transaction detection that identifies subscriptions and fixed monthly charges automatically. This eliminates the manual-categorization bottleneck and makes spend analysis possible in Phase 3.

## Scope

### In Scope — 5 chained PRs (#2–#6)

| PR | Deliverable | Lines | Target |
|---|-------------|-------|--------|
| **#2** | **Categories foundation** — `categories` table + seed 12 Y-NAB + migration 0005 + `Transaction.category_id`/`low_confidence` FK columns in migration 0006 + LLM prompt updated to emit closed-set category names + `_build_transactions` validates category against seed (miss → `category_id=NULL` + `low_confidence=True`) + `PATCH /api/v1/transactions/{id}` accepts `category_id` with write-through to denormalized `category` string + `GET /api/v1/categories` + `POST /api/v1/categories/{id}` stateless rename (single-transaction UPDATE on category row + write-through UPDATE on all matching transactions) | 250–400 | `main` |
| **#3** | **Categories UI** — replace free-text `<input>` with `<select>` sourced from `GET /api/v1/categories` + "Filter by category" multi-select in transactions filter form + `category_id` Query filter on `list_transactions` + "Uncategorized" as special filter (`category_id IS NULL OR low_confidence=True`) | 100–200 | PR #2 |
| **#4** | **Merchants + aliases** — `merchants` + `merchant_aliases` tables in migration 0006 (shared with PR #2's ALTER) + `Transaction.merchant_id` FK in 0006 + deterministic normalization (lowercase, strip `SUC.*`/digit noise/accents/punctuation/legal suffixes) + alias lookup + auto-create canonical on miss with `low_confidence=True` + opt-in LLM helper (`LLM_MERCHANT_NORMALIZATION_ENABLED`, default `false`) + `GET /api/v1/merchants` + `POST /api/v1/merchants/{id}/aliases` | 300–500 | PR #3 |
| **#5** | **Recurring detection** — `recurring_rules` table in migration 0007 + `Transaction.recurring_rule_id` FK in 0007 + `RecurringDetector` service at end of `ingest_statement` (always runs on `status=COMPLETED`; `logger.info` on full success, `logger.warning` on partial) + hybrid algorithm (same merchant, amount ±15%, ≥3 occurrences in 90 days, median interval → period) + skip installment rows + `confidence` REAL column (0.0–1.0) + `GET /api/v1/recurring` + `PATCH /api/v1/recurring/{id}` (is_active override) | 300–400 | PR #4 |
| **#6** | **Docs + E2E** — README Phase 2 section + `tests/test_e2e_phase2.py` (Santander PDF only) covering full happy path: upload → categorize → merchant normalize → recurring detected → user override via PATCH | 100–200 | PR #5 |

### Out of Scope

- Category hierarchy (parent/child) — flat 12 in v1
- Itaú or Banco de Chile E2E tests — Santander only per decision #13
- Sentry alerts or external notification for recurring detection — log only per decision #14
- Bulk category assignment from filter view — anti-feature per decision #12
- LLM merchant normalization enabled by default — opt-in flag per decision #6
- Audit trail or history table for category renames — stateless per decision #11
- Currency-split categories — single taxonomy for all currencies in v1

## Capabilities

### New Capabilities

- **phase2-categories**: Controlled taxonomy of 12 flat categories seeded at migration time; LLM ingestion emits from the closed set; PATCH endpoint accepts `category_id` with write-through; stateless rename propagates via single-transaction UPDATE; `low_confidence=True` flag for ambiguous assignments; UI `<select>` and multi-select filter. Covers PR #2 + #3.

- **phase2-merchant-aliasing**: Merchant canonicalization via deterministic normalization + alias-table lookup; unknown merchants auto-created with `low_confidence=True`; opt-in LLM helper for long-tail resolution; `merchant_id` FK on transactions. Covers PR #4.

- **phase2-recurring-detection**: Hybrid recurring detection at end of `ingest_statement`; explicit `confidence` column; always runs on success with log differentiation; is_active override; installments excluded. Covers PR #5.

### Modified Capabilities

None. The existing `ingestion-chunk-failure-tolerance` spec is not altered.

## Approach

5-PR chained plan with 3-migration atomicity (0005= categories, 0006= combined ALTER + merchants tables, 0007= recurring rules). Cherry-pick isolation gate enforced on PR #5: only one-line recurring detector call in ingestion.py.

## Affected Areas

| Area | Impact | PR |
|------|--------|----|
| `app/models/` — 4 new model files, `Transaction` extended | New + Modified | #2, #4, #5 |
| `app/schemas/domain.py` | Modified | #2, #4, #5 |
| `app/services/llm/prompts.py` | Modified | #2 |
| `app/services/ingestion.py` | Modified | #2, #5 |
| `app/services/merchants.py` (new) | New | #4 |
| `app/services/llm/merchant_helper.py` (new) | New | #4 |
| `app/services/recurring.py` (new) | New | #5 |
| `app/api/v1/` — 3 new endpoint modules, `transactions` extended | New + Modified | #2, #3, #4, #5 |
| `app/web/` — router + templates | Modified | #3 |
| `alembic/versions/0005-0007` | New | #2, #4, #5 |
| `README.md` + `tests/test_e2e_phase2.py` | New + Modified | #6 |

## Risks

Top 10 risks: cherry-pick isolation violation (medium), LLM token budget (low), taxonomy churn (low), recurring false-positives (medium), migration 0006 coordination (medium), installment false-positives (low), backward compat (low), merchant normalization over-aggression (low), test baseline regression (medium), recurring on partial-success (low).

## Rollback Plan

Per-PR Alembic downgrades drop columns and tables. Denormalized `category` string column never dropped. Feature-flagged LLM helper disabled by default.

## Dependencies

Cherry-pick (merged at 8c1e3dd), 14 product decisions (engram #53), explore synthesis (engram #52).

## Success Criteria

- All 5 PRs merged; sdd-verify passes per PR
- Cherry-pick isolation gate holds for PR #5
- 366-test baseline passes; 91.74% coverage floor maintained
- ruff + mypy clean; 3 migrations round-trip; E2E passes
