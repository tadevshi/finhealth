# Tasks: Phase 2 PR #6 — Documentation & E2E Test

## Summary

Total LOC: ~400 | Work units: 3 | PR boundary: single (no chain) | Tests: 1 new e2e (~250 LOC)

## Review Workload Forecast

| Field | Value |
|-------|-------|
| Estimated changed lines | ~400 |
| 400-line budget risk | Low |
| Chained PRs recommended | No |
| Suggested split | single PR |
| Delivery strategy | single-pr |
| Chain strategy | N/A |
| Review budget lines | 800 |

Decision needed before apply: No
Chained PRs recommended: No
Chain strategy: N/A
400-line budget risk: Low

### Suggested Work Units

| Unit | Goal | Likely PR | Notes |
|------|------|-----------|-------|
| 1 | README update | PR #6 | docs only; ~50-80 LOC |
| 2 | E2E test | PR #6 | ~250 LOC new test |
| 3 | SDD artifacts | PR #6 | required for archive |

## Phase 1: README documentation

### 1.1 `docs: update README with Phase 2 section, endpoints, migrations, status`
**Files**: `README.md`

- [x] Update top-of-file "Status" (line 7-10): "Phase 1" → "Phase 1 + Phase 2"; remove "Phase 2 — Classification" from "Coming next" (live now).
- [x] Insert "Available now (Phase 2)" section. One paragraph per capability: `phase2-categories` (12 Y-NAB categories seeded at migration, validated at LLM+ingestion+API, stateless rename), `phase2-merchant-aliasing` (regex normalizer + alias-table hit-or-create, ~80% via `KNOWN_MERCHANT_PATTERNS`), `phase2-recurring-detection` (detector at end of every successful ingest, 90-day window, ≥3 within ±15%, `confidence` 0.0-1.0).
- [x] Cross-reference 3 spec paths: `openspec/specs/phase2-categories/spec.md`, `phase2-merchant-aliasing/spec.md`, `phase2-recurring-detection/spec.md` (last is in `openspec/changes/phase-2-pr5-recurring-detection/specs/` until PR #5 archive step).
- [x] Add 6 endpoints to v1 table: `GET /api/v1/categories` (PR #2), `POST /api/v1/categories/{id}` (PR #2 — `POST` per `app/api/v1/categories.py:82`), `GET /api/v1/merchants` (PR #4), `POST /api/v1/merchants/{id}/aliases` (PR #4), `GET /api/v1/recurring` (PR #5), `PATCH /api/v1/recurring/{id}` (PR #5).
- [x] Mention migrations `0005_phase2_categories`, `0006_phase2_merchants_transactions_alter`, `0007_phase2_recurring_rules` by filename.
- [x] Every claim verifiable in `app/api/v1/{categories,merchants,recurring}.py` or specs. `ruff format README.md` clean.

## Phase 2: E2E test

### 2.1 `test(e2e): add Phase 2 happy-path test on Santander fixture`
**Files**: `tests/test_e2e_phase2.py` (new, ~250 LOC)

- [x] Mirror `tests/test_e2e_phase1.py` (imports, `FakeLLMClient`, `CANNED_NACIONAL_EXTRACTION`, `SANTANDER_PDF`, skip markers, fixtures).
- [x] New fixture `seeded_lider_history`: 1 `CreditCard` (Santander) + 1 `Merchant` (`name='lider'`, `default_category_id=<groceries.id>`, `low_confidence=False`) + 2 historical `Transaction` rows on same `credit_card_id` dated 60d and 30d back, `amount=Decimal("12450")`, `currency="CLP"`. Canned 3rd LIDER row trips the 3-occurrence threshold.
- [x] `test_phase2_happy_path_end_to_end` (`@needs_sample_pdf` + `@needs_test_rut` + `@pytest.mark.asyncio`): upload Santander PDF; assert (1) 201 + 3 transactions each with `merchant_id` + `category_id` + `recurring_rule_id` UUIDs; (2) `GET /api/v1/recurring` returns 1 rule, `period_label="monthly"`, `confidence >= 0.0`, `occurrences >= 3`, `merchant_id=<lider.id>`; (3) `PATCH /api/v1/recurring/{id}` with `{"is_active": false}` → 200; (4) subsequent `GET /api/v1/recurring` → `[]`; (5) 3 LIDER transactions still carry `recurring_rule_id` after deactivation.
- [x] `ruff check` + `ruff format` clean. `mypy` excluded on `tests/` per `pyproject.toml`.

## Phase 3: SDD artifacts

### 3.1 `chore(sdd): write verify-report.md and apply-progress.md`
**Files**: `openspec/changes/phase-2-pr6-docs-e2e/verify-report.md` (new), `apply-progress.md` (new)

- [x] `verify-report.md` status PASS. Cross-walk SC1/SC2/SC3 → commit SHAs, diff ranges, pytest output.
- [x] `apply-progress.md`: Tasks 1-3 complete; commit SHAs; pytest result; diff stats; ruff exits.
- [x] Both files exist.

## Reconciliation Note (added at archive time)

The task checkboxes in this file were not updated by `sdd-apply` after implementation. The orchestrator authorized archive-time reconciliation per the sdd-archive skill's exception clause. Proof of completion:

- `apply-progress.md` documents all 3 planned tasks + 1 gate-correction task with commit SHAs.
- `verify-report.md` reports status PASS (after the LIDER_CANNED_DATE fix).
- `git log` on `feat/phase2-pr6-docs-e2e` shows 5 commits: `71fdf68`, `4d73e86`, `6b991f4`, `71e0955`, `3b28987`.
- Full test suite: 340 passed, 74 skipped, 0 failed.
- ruff check + format: clean.

Reconciliation date: 2026-07-08. Reconciled by: sdd-archive (per orchestrator authorization).
