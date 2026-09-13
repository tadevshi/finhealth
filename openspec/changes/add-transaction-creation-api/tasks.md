# Tasks: add-transaction-creation-api

## Review Workload Forecast

| Field | Value |
|-------|-------|
| Estimated changed lines | Remaining implementation ~1,700–2,500 authored lines; PR1 already complete at ~250 authored lines and unchanged |
| 400-line budget risk | High |
| Chained PRs recommended | Yes |
| Suggested split | PR 1 complete: merchant savepoint safety → PR 2: migration/model/source/statement response compatibility → PR 3: nested metadata schemas and internal parent-planning service → PR 4: atomic statement+transaction persistence and routes → PR 5: races/rollback hardening, PDF regressions, docs, full verification |
| Delivery strategy | ask-on-risk |
| Chain strategy | feature-branch-chain |

Decision needed before apply: Yes
Chained PRs recommended: Yes
Chain strategy: feature-branch-chain
400-line budget risk: High

> Under `ask-on-risk`, more than two remaining PRs are now required to keep review slices near the 400 authored-line budget. Pause before apply for explicit confirmation of the expanded remaining chain (`PR2`–`PR5`) or an explicit `size:exception`; do not infer either. PR1 is complete and must remain unchanged.

## Scope guardrails

- [ ] Do not edit source code or legacy OpenSpec changes while performing this tasks phase; future apply must not rewrite PR1 work.
- [ ] Preserve strict TDD for every remaining work unit: RED → GREEN → TRIANGULATE → REFACTOR, with command/results recorded in `openspec/changes/add-transaction-creation-api/apply-progress.md`.
- [ ] Use PostgreSQL-backed evidence for migrations, atomicity, uniqueness, concurrency, and PDF regressions; skipped database tests are not proof.
- [ ] Keep transaction creation statement-linked: every item supplies non-null `statement_id`.
- [ ] Preserve single and batch creation, category/currency/merchant/atomicity semantics, existing GET/PATCH behavior, and unchanged PDF upload/file-saving/hash/dedup/lifecycle behavior.
- [ ] Keep reconciliation, idempotency keys, automatic retry/deduplication, recurring detection changes, standalone statement creation, statement-less transactions, PDF-storage removal, and transaction-level provenance migration out of scope.

## 1. PR1 complete: deterministic merchant savepoint safety

### 1.1 Preserve completed work unchanged

- [x] Treat `app/services/merchants.py` deterministic savepoint safety and its `tests/test_merchants.py` PostgreSQL regressions as already implemented in PR1.
- [x] Do not re-slice, rewrite, or expand PR1 while implementing later PRs.
- [x] Later PRs may depend on PR1 behavior but must add their own creation-flow evidence instead of modifying PR1 scope.
- [x] Rollback boundary remains PR1-only: revert the merchant savepoint fix and PR1 tests without removing transaction creation work.

## 2. PR2 candidate: statement persistence compatibility and provenance

### 2.1 RED: migration/model/source expectations

- [x] Add failing tests in `tests/test_alembic.py` and/or `tests/test_models.py` for populated-baseline upgrade preserving existing `file_path`/`file_hash`, backfilling source `pdf`, allowing null file fields for API statements, retaining `uq_statements_credit_card_id_file_hash` for non-null hashes, allowing multiple null hashes, rejecting invalid/null source, and safe downgrade refusal when API/null-file rows exist.
- [x] Add failing migration-runner tests in `tests/test_alembic.py` for versioned forward traversal, head no-op/downgrade behavior, unversioned populated refusal, unknown revision failure, offline SQL guard coverage, and baseline immutability.
- [x] Record RED PostgreSQL command/results.

### 2.2 GREEN: nullable files and explicit statement source

- [x] Update `app/models/statement.py` and `app/models/__init__.py` with nullable `file_path`/`file_hash`, `StatementSource` values `pdf`/`api`, non-native enum/check/default alignment, and explicit PK-name parity if required by design.
- [x] Add `alembic/versions/0002_statement_source.py` after `0001_postgresql_baseline` to backfill `source='pdf'`, keep a PDF server/application default, drop file NOT NULL constraints, preserve the card/hash uniqueness constraint, and guard downgrade without deleting/fabricating data.
- [x] Narrowly update `alembic/env.py` so recognized versioned databases can traverse migrations and downgrades while populated unversioned databases remain refused.
- [x] Run focused migration/model tests and record GREEN evidence.

### 2.3 RED/GREEN: source-aware readers and PDF preservation

- [x] Add failing tests for `StatementResponse` null file serialization and `source`, statement GET compatibility for API-style rows, and PDF upload returning/persisting `source=pdf` with non-null file metadata.
- [x] Update `app/schemas/domain.py` and exports so `StatementResponse` exposes nullable `file_path`/`file_hash` and source while keeping PDF-specific `StatementCreate` path/hash requirements intact.
- [x] Update `app/services/ingestion.py` to explicitly assign PDF source without changing upload saving, hashing, deduplication, status, errors, recurring, or parsing behavior.
- [x] Audit `app/api/v1/statements.py`, `app/cli/seed_demo.py`, `app/web/`, and fixtures for narrow null/source compatibility; change only paths proven necessary.
- [x] TRIANGULATE with PDF dedup same-card/cross-card behavior, multiple null-hash API rows, and unchanged upload bytes/hash behavior.
- [x] REFACTOR only for compatibility/readability; run `tests/test_alembic.py`, `tests/test_models.py`, relevant statement/PDF tests, `ruff`, and `mypy --strict app/` as this PR's evidence.
- [x] Rollback boundary: revert the 0002 migration, statement model/source/schema compatibility, ingestion source assignment, and PR2 tests before any accepted API-created null-file rows exist.

## 3. PR3 candidate: nested metadata schemas and internal parent planning

### 3.1 RED: closed creation schemas and nested statement metadata

- [x] Add failing schema tests in `tests/test_transaction_creation.py` for `StatementMetadataCreate` requiring `credit_card_id`, `period_start`, `period_end`, `statement_date`; rejecting nested `id`, files, source, status, errors, currency, timestamps, and unknown fields; allowing statement date outside period; rejecting `period_start > period_end`; treating nested `statement: null` as omitted.
- [x] Add failing schema tests for `TransactionCreate.statement`, required non-null `statement_id`, optional `category_id`, closed top-level input, Decimal string/integer acceptance, float/bool/non-finite money rejection, installment bounds/overflow, optional `raw_json`, and malformed UUID/date/currency cases.
- [x] Add failing batch schema tests for `TransactionBatchCreate` 0/1/200/201 bounds and indexed nested validation locations.
- [x] Record RED evidence.

### 3.2 GREEN: schema additions without persistence behavior

- [x] Update `app/schemas/domain.py` and `app/schemas/__init__.py` with `StatementMetadataCreate`, extended `TransactionCreate`, creation-only money validators, bounded `TransactionBatchCreate`, and `TransactionBatchResponse`.
- [x] Keep `TransactionResponse` unchanged and keep PDF input schemas source-appropriate.
- [x] Run focused schema tests and record GREEN evidence.

### 3.3 RED: request-wide parent metadata planning rules

- [x] Add failing service-unit tests for a pure/internal parent-planning helper in `app/services/transaction_creation.py`: existing `statement_id` plus nested metadata returns 409 even when matching; missing parent with no metadata returns 422; exactly one metadata object may appear anywhere for a new ID; duplicate identical or conflicting metadata returns 422 at the second metadata-bearing index; mixed existing/new statement IDs resolve independently; lowest offending index wins.
- [x] Add failing tests for unknown new-parent card 404, card/currency lookup preparation, no writes during planning failures, and null metadata followers sharing the one authoritative object.
- [x] Record RED evidence.

### 3.4 GREEN/TRIANGULATE: internal service skeleton and parent planner

- [x] Create `app/services/transaction_creation.py` with `TransactionCreationService`, a small service exception (`code`, safe `message`, optional `field`, optional zero-based `index`), and an internal parent-planning path that starts one outer transaction before reads but does not expose routes yet.
- [x] Batch-fetch persisted statements/cards/categories needed by planning; validate parent definitions before dependent writes; keep existing statement metadata/source/status/errors unchanged.
- [x] TRIANGULATE metadata appearing first/middle/last, repeated batch items sharing one metadata object, multiple new parents, existing-parent conflict precedence, and missing metadata 422.
- [x] REFACTOR helper names and test fixtures only after focused tests pass.
- [x] Run `pytest tests/test_transaction_creation.py` with PostgreSQL settings plus `ruff`/`mypy`; record results.
- [x] Rollback boundary: remove schema additions and the internal transaction creation service skeleton/tests; PR2 source/nullable statement compatibility remains intact.

## 4. PR4 candidate: atomic statement+transaction creation and public routes

### 4.1 RED: domain policy and persistence behavior

- [ ] Add failing PostgreSQL service tests for creating new statements under caller UUID with source `api`, status `completed`, null file fields/errors, supplied card/dates, no ingestion/PDF/recurring work, and no changes to existing statement source/status/errors/timestamps.
- [ ] Add failing tests for single and batch transaction persistence: category ID precedence, legacy category preservation, no-category low confidence, unknown category 404, exact CLP/USD support, card currency mismatch 400, inactive/non-completed existing parent acceptance, out-of-period transaction acceptance, and ordered outputs.
- [ ] Add failing tests for deterministic merchant reuse/creation under creation flow, long-description merchant guard, no LLM invocation, and merchant defaults not inferring transaction category.
- [ ] Record RED evidence.

### 4.2 GREEN: atomic service persistence

- [ ] Complete `TransactionCreationService.create_many(items)` so new statements are inserted in canonical UUID order and flushed before transaction enrichment; catch only statement PK `23505` conflicts as `statement_creation_conflict` 409 and let all other database failures abort generically.
- [ ] Validate category/currency/merchant policy, build transaction rows in input order, flush once, build `TransactionResponse` snapshots before commit, and return snapshots only after the outer transaction succeeds.
- [ ] Ensure all request-created statements, transactions, merchants, and aliases share one outer transaction; no service or normalizer path commits caller work.
- [ ] Run focused service tests and record GREEN evidence.

### 4.3 RED/GREEN: HTTP routes and indexed errors

- [ ] Add failing HTTP tests for `POST /api/v1/transactions` and `POST /api/v1/transactions/batch`: existing parent ID-only 201, new parent 201, mixed batch 201, input-order responses, 1/200 bounds, duplicate-looking rows and repeated successful submissions not deduplicated, visibility through existing GET, and batch count accuracy.
- [ ] Add failing HTTP tests for 422 schema/content errors, missing metadata, duplicate metadata, 409 existing-parent metadata, 404 unknown card/category, 400 currency business rule failures, indexed batch domain errors, and generic 500 bodies with `raise_app_exceptions=False`.
- [ ] Update `app/api/v1/transactions.py` with thin POST handlers using existing router/session dependency and local service-error mapping; preserve existing GET/PATCH transport and response contracts.
- [ ] Run focused HTTP tests and record GREEN evidence.

### 4.4 TRIANGULATE/REFACTOR and PR4 regression

- [ ] Add compatibility regressions for transaction GET filters, PATCH form/HTML/JSON response behavior, PATCH empty-string clear sentinel, category tests, merchant API tests, and ingestion tests remaining unchanged.
- [ ] TRIANGULATE batch size 200 success/201 failure without writes, duplicate-looking items in the same batch creating distinct IDs, and API additions to existing PDF statements leaving statement source `pdf`.
- [ ] REFACTOR service/route helpers for readability only; maintain one service path for single and batch.
- [ ] Run focused creation/transaction/merchant tests, `ruff check app tests`, `ruff format --check app tests`, and `mypy --strict app/`; record results.
- [ ] Rollback boundary: remove POST route wiring and creation-service persistence behavior while retaining PR2 statement compatibility for any accepted API-created rows.

## 5. PR5 candidate: race/rollback hardening, PDF regression, docs, and full verification

### 5.1 RED/GREEN: request-wide atomicity failures

- [ ] Add failing PostgreSQL tests that inject failures after new parent flushes, after merchant/alias writes, during transaction flush, during response snapshot validation, and before/during commit; assert with a fresh independent session that no request-created statement, transaction, merchant, or alias rows survive.
- [ ] Include late invalid batch items, multiple flushed new parents, recovered merchant conflicts followed by later failure, and existing/concurrent parent conflicts; assert pre-existing rows and independently committed winners remain.
- [ ] Fix only `app/services/transaction_creation.py` or narrowly necessary shared code so failures propagate out of the transaction context and routes never return partial 201.
- [ ] Record RED/GREEN PostgreSQL evidence.

### 5.2 RED/GREEN: real concurrent new-statement races

- [ ] Add two-session/barrier PostgreSQL tests where concurrent requests attempt the same new `statement_id` with matching and different valid metadata; both observe absence, one succeeds, loser receives atomic 409, and loser request-created rows are absent.
- [ ] Add tests for reversed multi-parent input order, earlier inserted loser parent(s), winner rollback allowing the other insert to succeed, no automatic retry/reuse, explicit ID-only retry success, and resending metadata after success still returning 409.
- [ ] Ensure conflict mapping uses exact statement PK uniqueness and does not classify card/hash/FK/deadlock/connection errors as parent races.
- [ ] Record RED/GREEN evidence.

### 5.3 TRIANGULATE: PDF, migration, and source regressions under full flow

- [ ] Run or add regressions proving PDF upload still saves identical bytes, hashes real content, returns `source=pdf`/non-null file metadata, deduplicates by card/hash, preserves lifecycle/failure/recurring behavior, and remains separate from similar API-created statements.
- [ ] Re-run migration downgrade/upgrade, nullable/source, unsafe downgrade, and uniqueness tests after routes exist.
- [ ] Verify API calls create no files and never schedule PDF, LLM, recurring, reconciliation, or idempotency behavior.
- [ ] Record focused PostgreSQL/PDF evidence; if real PDF E2E prerequisites (`TEST_RUT`, sample PDFs) are unavailable, record unavailable rather than passed.

### 5.4 RED/GREEN: documentation

- [ ] Add executable documentation checks if available, or record a manual RED checklist for `README.md` requiring existing-parent ID-only examples, new-parent nested metadata examples, mixed batch/shared metadata rules, 409 retry with ID-only linkage, nullable statement response fields/source, API completed-status meaning, category/currency/merchant behavior, 1–200 batch limit, append-only/non-idempotent/timeout duplicate caveats, and no reconciliation/recurring/PDF-storage-removal promises.
- [ ] Update `README.md` with concise JSON examples and caveats; avoid implying statement source is transaction-level provenance.
- [ ] Run `./scripts/verify.sh` when docs/config/web are touched, or record exact unavailable prerequisites.

### 5.5 REFACTOR and final verification

- [ ] Remove duplicated fixtures/helpers only after behavior remains green; do not weaken PostgreSQL-backed assertions.
- [ ] Run focused command: `POSTGRES_TEST_HOST=127.0.0.1 POSTGRES_TEST_PORT=5432 POSTGRES_TEST_USER=finhealth POSTGRES_TEST_PASSWORD=secret pytest tests/test_transaction_creation.py tests/test_merchants.py tests/test_transactions.py tests/test_alembic.py tests/test_models.py tests/test_ingestion.py tests/test_web_phase1.py`.
- [ ] Run complete pytest suite with explicit PostgreSQL test settings; skipped database tests are not accepted for migration/atomicity/race claims.
- [ ] Run `ruff check app tests`, `ruff format --check app tests`, `mypy --strict app/`, and `./scripts/verify.sh`; record exact results.
- [ ] Rollback boundary: remove creation docs and race/rollback hardening added in PR5 only if PR4 exposure is also disabled; retain source-aware readers and accepted data compatibility.

## 6. Final apply/verify bookkeeping

- [ ] Update `openspec/changes/add-transaction-creation-api/apply-progress.md` after each PR/work unit with RED/GREEN/TRIANGULATE/REFACTOR evidence, focused commands/results, runtime scenario/results or justified N/A, changed-line counts, and rollback boundary.
- [ ] Update `openspec/changes/add-transaction-creation-api/verify-report.md` with final focused, PostgreSQL integration, race, migration, PDF, lint, format, typecheck, docs, and full-suite results.
- [ ] Confirm `openspec/changes/add-transaction-creation-api/proposal.md`, `design.md`, and `specs/transactions-api/spec.md` still match delivered behavior before archive.
- [ ] Confirm delivered code contains no statement-less creation, synthetic placeholders, reconciliation, idempotency, automatic retry deduplication, recurring changes, PDF-storage removal, transaction-level provenance migration, or PATCH behavior changes.
