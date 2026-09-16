# Verification Report: add-transaction-creation-api

## Result

**PASS with pre-existing warnings.** PR1–PR5 are merged into tracker #66. No CRITICAL findings were introduced by this change.

## Evidence

- Focused PostgreSQL suite: **285 passed, 50 skipped in 57.57s**.
- Complete PostgreSQL-configured suite: **702 passed, 1 pre-existing failure, 74 skipped in 151.24s**.
- `ruff check app tests`: **passed**.
- `ruff format --check app tests`: **warning**; two pre-existing files would be reformatted: `app/services/pdf/text_truncator.py`, `tests/test_pdf_services.py`.
- `mypy --strict app/`: **6 pre-existing baseline errors**, matching the baseline; no chain-touched file introduced an error.
- `./scripts/verify.sh`: **exit 0**; Ruff, compileall, docs, and Docker checks passed.
- Race tests: two-session/barrier matching and differing metadata, loser rollback, winner rollback, reversed parent order, exact PK/SQLSTATE discrimination, and ID-only retry all pass.
- Atomicity tests: failure after parent flush, merchant/alias writes, transaction flush, response snapshot validation, and before commit all prove durable-state absence using fresh sessions.
- Migration/source tests: upgrade, downgrade refusal, nullable files, source defaults/checks, uniqueness, and PDF/API separation pass.
- Documentation checks pass; README documents both POST endpoints and explicit non-goals.

## Unavailable prerequisites

Real encrypted-PDF E2E tests remain unavailable because `TEST_RUT` and local sample PDFs are not present. These skips are recorded as unavailable, never treated as passing evidence. Ungated PDF/model/source regressions pass.

## Pre-existing warnings

1. `test_dashboard.py::TestMonthly::test_monthly_zero_transaction_months_filled_in` is date-dependent and fails against the current date window; dashboard code is untouched by this change.
2. Two unrelated pre-existing files fail `ruff format --check`; they are outside the chain diff.

## Scope confirmation

The delivered code preserves mandatory `statement_id`, creates API statements as `source=api` and `completed` with nullable file metadata, preserves PDF `source=pdf` behavior, and does not add statement-less creation, synthetic placeholders, reconciliation, idempotency, automatic retry deduplication, recurring changes, PDF storage removal, transaction-level provenance migration, or PATCH behavior changes.

## Recommendation

Ready for tracker closure after the pre-existing warnings are accepted as non-blocking and the tracker PR is marked ready/merged according to repository policy.
