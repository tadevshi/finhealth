# Apply Progress: add-transaction-creation-api

## Status: PR 1 complete — Ready for verify (PR 1 slice)

PR 1 of the feature-branch-chain delivery (deterministic merchant savepoint
safety) is implemented under strict TDD with real PostgreSQL evidence. PR 2
(schemas/service/routes) and PR 3 (atomicity hardening + docs) are not started.

Delivery decision consumed from the parent prompt (recorded here per the
`ask-on-risk` gate): chained PRs via `feature-branch-chain`; this apply executed
only the PR 1 work unit with a 250-changed-line slice budget.

## Changed lines (PR 1 slice)

| File | Additions | Deletions |
|---|---|---|
| `app/services/merchants.py` | 60 | 56 |
| `tests/test_merchants.py` | 132 | 2 |
| **Authored total (additions + deletions)** | **250** | |

Measured with `git diff --numstat` (the pre-existing `.gitignore` modification
existed before this work unit and is excluded). At the 250-line slice budget.

## TDD Cycle Evidence

Command prefix for every run below:

```sh
POSTGRES_USER=finhealth POSTGRES_PASSWORD=secret POSTGRES_DB=finhealth \
POSTGRES_TEST_HOST=127.0.0.1 POSTGRES_TEST_PORT=5432 \
POSTGRES_TEST_USER=finhealth POSTGRES_TEST_PASSWORD=secret pytest
```

Environment note: the compose `postgres` service was not running at session
start; it was started (`docker compose up -d postgres`) and the existing
volume's role password was aligned to the canonical test password with
`ALTER USER finhealth WITH PASSWORD 'secret'` (non-destructive). No skipped
database tests were used as evidence; every conflict test ran against a real,
isolated PostgreSQL 16 database created by `tests/conftest.py`.

### RED (task 1.1)

Command: `... pytest tests/test_merchants.py::TestDeterministicConflictRecovery -q`
Result: `3 failed, 1 passed in 1.46s`

| Failing test | Failure | Hazard demonstrated |
|---|---|---|
| `test_merchant_name_conflict_preserves_pending_caller_work` | `assert 0 == 1` — pending statement count is 0 after commit | A real `UniqueViolationError` on `ix_merchants_name` fired (winner committed with no alias, so the alias lookup misses), and the handler's `session.rollback()` discarded the caller's flushed pending statement |
| `test_raw_alias_conflict_recovers_to_existing_winner` | `UniqueViolationError ... "merchant_aliases_alias_text_key"` propagates | The raw-alias conflict raised instead of recovering; the old re-query used the non-unique `normalized` key and missed |
| `test_non_target_integrity_error_propagates` | `NoResultFound` raised instead of `IntegrityError` | The old catch-all swallowed a non-unique FK failure, rolled back caller work, then crashed on the winner re-query |

The passing test (`test_ambiguous_normalized_alias_does_not_pick_a_winner`) is
an intentional characterization pin of existing intended behavior (ambiguous
normalized aliases propagate `MultipleResultsFound`); it was green before and
after the change.

### GREEN (task 1.2)

Change: only the two deterministic insert blocks in `resolve_merchant`. Each
now (a) flushes caller-owned pending work before the guarded insert, (b) adds
and flushes the candidate inside `async with session.begin_nested()`, (c) on
`IntegrityError` recovers only when `_is_target_unique_violation` matches
SQLSTATE 23505 AND the exact violated key (`ix_merchants_name` for the merchant
name; `merchant_aliases_alias_text_key` for the raw alias text), re-queries and
verifies the winner (`scalar_one_or_none`, re-raise on miss), and never calls
`Session.rollback()` or `commit()`. Alias recovery re-queries by the exact raw
`alias_text`, not `normalized`. Return flags preserved: merchant-name conflict
recovery still falls through to alias creation and returns `was_new=True`
(ingestion low-confidence semantics unchanged); alias-conflict recovery returns
`(winner.merchant, False)` as before. `resolve_merchant_with_llm` is untouched
(its rollback hazard is explicitly out of scope per design).

Driver detail discovered during GREEN: SQLAlchemy's asyncpg adapter wraps the
driver error; `sqlstate` is on `exc.orig` while `constraint_name` is only on
`exc.orig.__cause__` — the helper checks both.

Command: `... pytest tests/test_merchants.py -q --no-cov`
Result: `34 passed in 4.42s` (was 30 before this work unit; +4 new tests)

### TRIANGULATE (task 1.3)

- Merchant-name conflict: pending ingestion-style `Statement` (pending bank →
  card → statement chain, flushed, uncommitted) survives recovery and commit;
  exactly one `mcdonalds` merchant; new alias bound to the winner.
- Raw-alias conflict: recovery binds to the winner merchant via the exact raw
  key; the candidate `paris` merchant created inside the failed savepoint is
  rolled back (`count == 0`); pending statement survives.
- Non-target FK failure: `IntegrityError` propagates; no merchant persisted.
- Ambiguous normalized aliases: `MultipleResultsFound` propagates (no silent
  winner choice).
- Lookup-hit behavior, empty canonical descriptions, and source/default
  category flags remain covered by the pre-existing tests in this file
  (`test_alias_lookup_second_upload_hits_existing`,
  `test_resolve_merchant_empty_description`,
  `test_alias_lookup_first_upload_creates_merchant_and_alias`, LLM-source
  assertions), all green.

Command: `... pytest tests/test_merchants.py -q --no-cov`
Result: `34 passed in 4.71s`

### REFACTOR (task 1.4)

Extracted the shared SQLSTATE/constraint-name discrimination into
`_is_target_unique_violation` (removes duplicated checks between the two
blocks) and compressed comments to the transaction-boundary essentials. No
behavior change after refactor.

## Verification evidence

| Command | Result |
|---|---|
| `... pytest tests/test_merchants.py -q` (default coverage addopts) | `34 passed in 28.63s` |
| `... pytest tests/test_merchants.py tests/test_ingestion.py tests/test_transactions.py -q --no-cov` | `86 passed, 50 skipped in 21.07s` — skips are exclusively `TEST_RUT` real-PDF E2E prerequisites (unavailable in this environment; recorded as unavailable, not passed) |
| `ruff check app/services/merchants.py tests/test_merchants.py` | All checks passed |
| `ruff format --check app/services/merchants.py tests/test_merchants.py` | 2 files already formatted |
| `mypy --strict app/services/merchants.py` | 0 errors in this file (full `mypy --strict app/` has 6 pre-existing baseline errors elsewhere; none introduced here — the 2 transient errors found mid-cycle were fixed by renaming the alias-winner variable) |

Ingestion and transaction regression suites pass unchanged, confirming
ingestion semantics and existing GET/PATCH behavior are preserved.

## Deviations from design

- The alias insert block does not repeat the explicit pre-flush: no caller code
  runs between the two blocks, caller-owned pending work is already flushed
  before the merchant savepoint, and `begin_nested()` still flushes implicitly
  before establishing the savepoint. The constraint-name check would re-raise
  any non-target failure regardless.
- The `was_new=True` flag on the merchant-name conflict recovery path is
  intentionally preserved (design: "preserve existing return flags"); the
  creation service in PR 2 ignores this flag.

## Workload / PR boundary

- PR boundary: deterministic normalizer safety + its regressions only. No
  creation schemas, service, routes, or README changes (PR 2/PR 3 scope).
- Changed-line count: 250 authored (additions + deletions), at the slice budget.
- Rollback boundary: revert `app/services/merchants.py` and the
  `TestDeterministicConflictRecovery` additions in `tests/test_merchants.py`
  (plus the import-line updates in that file). No transaction-creation files
  exist yet, so nothing else is affected. No commit was made.

## Remaining tasks (exact unchecked lines from tasks.md)

All PR 1 tasks (1.1–1.4) are checked. Remaining unchecked work belongs to PR 2,
PR 3, and final bookkeeping: sections 2.1–2.8, 3.1–3.6, the three scope
guardrails, and section 4 (`apply-progress.md`/`verify-report.md` finalization,
spec/design consistency confirmation, no-statement-less-source confirmation).

## Structured status

- Change: `add-transaction-creation-api`; artifact store: openspec (Engram
  unavailable in this session — decisions and evidence are persisted in this
  file and in `tasks.md` checkbox updates only; no memory-tool persistence is
  claimed).
- `actionContext`: repo-local, workspace root
  `/home/tadashi/orca/workspaces/finhealth/transactions-api`; all edits stayed
  inside the allowed edit roots and the four allowed surfaces.
- The native status engine reported ambiguous change selection at session
  start; the parent prompt explicitly resolved the change and the PR 1 work
  unit with attempt authority, so apply proceeded on that basis.
- Skill resolution: `paths-injected` (gentle-ai, work-unit-commits,
  cognitive-doc-design SKILL.md files read before work; no registry discovery,
  no child subagents spawned).

## Status: PR 2 complete — Ready for verify (PR 2 slice)

PR 2 (statement persistence compatibility and provenance) is implemented
under strict TDD with real PostgreSQL evidence. PR1 merchant work is
untouched (`app/services/merchants.py`, `tests/test_merchants.py`
unmodified in this unit). PR3–PR5 are not started.

Delivery decision consumed from the parent prompt (recorded here per the
`ask-on-risk` gate): `feature-branch-chain` with 5 PRs accepted; this
apply executed only the PR 2 work unit with a declared 350-changed-line
slice budget.

## Changed lines (PR 2 slice)

| File | Additions | Deletions |
|---|---|---|
| `alembic/versions/0002_statement_source.py` (new) | 106 | 0 |
| `alembic/env.py` | 21 | 12 |
| `app/models/statement.py` | 79 | 19 |
| `app/models/__init__.py` | 2 | 1 |
| `app/schemas/domain.py` | 20 | 4 |
| `app/schemas/__init__.py` | 2 | 0 |
| `app/services/ingestion.py` | 4 | 1 |
| `tests/test_alembic.py` | 406 | 3 |
| `tests/test_models.py` | 131 | 0 |
| `tests/test_ingestion.py` | 73 | 1 |
| **Authored total (additions + deletions)** | **888** | |

Measured with `git diff --numstat` plus the new-file line count. **This
slice exceeds the declared 350-line budget (888 authored lines).** Two
trimming passes removed ~150 lines (merged redundant migration-runner and
model tests; the duplicate unversioned-populated-refusal test was dropped
because the pre-existing `test_preseeded_database_fails_before_baseline_ddl`
already pins that behavior). The remaining overage is structural: the
tasks' PR2 scope enumerates ~15 distinct PostgreSQL-backed behaviors
(upgrade backfill, nullable files, multiple null hashes, preserved
non-null uniqueness, source CHECK/NOT NULL, guarded downgrade refusal and
safe downgrade, runner traversal/no-op/unknown-revision/offline-guard
coverage, source-aware response evolution). **Budget-breach decision
referred to the parent** under `ask-on-risk`: either re-slice PR2 into
2a (migration/model/runner) and 2b (readers/source) as separate chain
entries, or explicitly accept `size:exception` before PR creation. No
commit was made, so re-slicing before the PR boundary is still possible.

## TDD Cycle Evidence (PR 2)

Command prefix for every run below:

```sh
POSTGRES_USER=finhealth POSTGRES_PASSWORD=secret POSTGRES_DB=finhealth \
POSTGRES_TEST_HOST=127.0.0.1 POSTGRES_TEST_PORT=5432 \
POSTGRES_TEST_USER=finhealth POSTGRES_TEST_PASSWORD=secret pytest
```

### RED (tasks 2.1 + 2.3)

- `pytest tests/test_alembic.py -q --no-cov` → `10 failed, 4 passed in 2.50s`
  (lineage, populated upgrade, API rows, uniqueness, source check, refusal,
  safe downgrade, runner traversal, no-op/unknown-revision, offline guard
  all failing; pre-existing unversioned-refusal test green as a
  characterization pin).
- `pytest tests/test_models.py tests/test_ingestion.py -q --no-cov` →
  collection `ImportError: cannot import name 'StatementSource'` in both
  modules (model + exports absent).
- Reader failure after model landed but before schemas changed:
  `StatementResponse` rejected `file_path=None`
  (`string_type` validation error) — proving the response evolution gap.

### GREEN (tasks 2.2 + 2.3)

- `pytest tests/test_alembic.py -q --no-cov` → `11 passed in 2.86s` (later
  `14 passed`/`11 passed` after consolidation; final: `11 passed in 2.87s`)
- `pytest tests/test_models.py tests/test_ingestion.py::TestGetApiStyleStatementEndpoint -q --no-cov` → `31 passed` (16 models incl. 5 new;
  API-statement GET returns `source=api`, null files, completed).

### TRIANGULATE (task 2.3)

- PDF dedup: same-card non-null hash duplicate rejected with
  `UniqueViolationError`; cross-card same hash allowed (both at migrated
  head via raw SQL and at `create_all` schema via the pre-existing
  `test_file_hash_unique_per_credit_card`).
- Multiple same-card API statements with null hashes persist (migration
  level + ORM level).
- Upload bytes/hash behavior: full real-PDF upload E2E assertions
  (`source=pdf`, non-null file metadata in upload and GET responses) are
  wired into the `@needs_sample_pdfs @needs_test_rut` gated tests;
  prerequisites unavailable in this environment — recorded as
  unavailable, not passed. Un-gated evidence: seeded PDF-source row GET
  (models/ORM), ORM default `source=pdf`, and explicit ingestion
  assignment.
- Downgrade refusal verified row-by-row: refusal leaves `version_num=0002`,
  nullable files and the API row intact.
- Full-regression: `pytest tests/test_ingestion.py tests/test_merchants.py
  tests/test_transactions.py tests/test_web_phase1.py tests/test_seed_demo.py
  tests/test_seed_demo_postgres.py tests/test_db.py -q --no-cov` →
  `156 passed, 50 skipped in 34.78s`; focused PR2 set → `186 passed,
  50 skipped in 39.75s`; complete suite → `1 failed, 572 passed, 74 skipped
  in 174.41s` — the single failure
  (`test_dashboard.py::TestMonthly::test_monthly_zero_transaction_months_filled_in`)
  is a pre-existing, date-dependent failure reproduced with the PR2 diff
  stashed (fixtures anchor 2026-06/07 vs. system date 2026-09); unrelated
  to this unit.

### REFACTOR

- Merged three at-head behavior tests into one; dropped the redundant
  unversioned-refusal duplicate; compacted model tests (sources-default +
  API-row merged; constraint-name test made synchronous); tightened the
  0002 and statement module docstrings. No behavior change; all suites
  re-run green after refactor.

## Verification evidence

| Command | Result |
|---|---|
| `... pytest tests/test_alembic.py tests/test_models.py tests/test_ingestion.py tests/test_merchants.py tests/test_transactions.py tests/test_web_phase1.py tests/test_seed_demo.py tests/test_db.py tests/test_health.py -q --no-cov` | `186 passed, 50 skipped in 39.75s` (skips are exclusively TEST_RUT/sample-PDF E2E prerequisites — unavailable, not passed) |
| `... pytest` (complete suite, `--no-cov`) | `1 failed, 572 passed, 74 skipped in 174.41s`; single failure is the pre-existing date-dependent dashboard monthly test (reproduced with PR2 diff stashed) |
| `ruff check app tests` | All checks passed |
| `ruff format --check` (touched files) | Formatted (2 pre-existing unformatted files elsewhere are not part of this slice) |
| `mypy --strict app/` (clean cache) | 6 errors — all pre-existing baseline errors, none introduced (verified against baseline by stash) |
| Alembic offline downgrade (`0002_statement_source:0001_postgresql_baseline --sql`) | Emitted SQL contains `LOCK TABLE statements`, the `file_path IS NULL`/`file_hash IS NULL` guard, and the guard precedes any `ALTER TABLE` |

Runtime harness scenario: real PostgreSQL 16 disposable databases created
and dropped per test (`tests/conftest.py`, `tests/test_alembic.py`
fixtures); no skipped database test was used as migration/atomicity
evidence.

## Environment and implementation notes

- `Enum(..., create_constraint=True, name=...)` naming: SQLAlchemy 2.0.51
  uses the Enum's `name` for the non-native CHECK constraint; the named
  CHECK was instead declared explicitly in `__table_args__`
  (`ck_statements_source`) with `create_constraint=False` on the type so
  ORM `create_all` DDL matches the migration exactly.
- Migration offline mode: `op.get_bind()` returns a MockConnection in
  offline mode, so the guarded downgrade branches on
  `context.is_offline_mode()` and emits equivalent `LOCK TABLE` + guard
  SQL for offline scripts; online mode uses a live guard that raises
  before any DDL inside the migration transaction.
- Alembic downgrade at head was silently bypassed by the previous
  at-head early return in `alembic/env.py`; the runner now guards only
  unversioned populated databases and lets Alembic traverse upgrades,
  no-ops and downgrades.
- `statement.source = "web"` invalid-value enforcement surfaces as
  `IntegrityError` from the DB CHECK (`ck_statements_source`) rather than
  Python-side enum validation; the model test pins that behavior.
- `app/api/v1/statements.py`, `app/cli/seed_demo.py`, `app/web/`: audited —
  no file-field dereference or response-key coupling found; no changes
  required (ORM/server default `pdf` covers seed/demo rows).
- A transient `mypy` "unused ignore" on `app/api/v1/router.py` was a
  stale-cache artifact: with a clean `.mypy_cache` the original file is
  clean; router.py was restored byte-identical to HEAD.

## Deviations from design

- None material. The design's downgrade guard is implemented as SQL that
  works both online (executed, raising inside the migration transaction)
  and offline (emitted into the SQL script), satisfying "emit equivalent
  lock/guard SQL for offline downgrade" with one implementation. The
  offline guard message wording differs from the online `RuntimeError`
  text; both match the "Cannot downgrade" refusal contract.

## Workload / PR boundary

- PR boundary: statement persistence compatibility and provenance only —
  model/source/nullable files, migration 0002 with guarded downgrade,
  migration-runner traversal, source-aware statement responses, explicit
  ingestion PDF source, and their tests. No transaction-creation
  endpoints, no creation service, no PR1 merchant changes, no README/docs
  changes (PR3–PR5 scope).
- Changed-line count: 888 authored (additions + deletions) — **exceeds the
  350-line slice budget**; parent decision required before PR creation
  (see Changed lines section).
- Rollback boundary: revert `alembic/versions/0002_statement_source.py`,
  the statement model/source/schema compatibility changes
  (`app/models/statement.py`, `app/models/__init__.py`,
  `app/schemas/domain.py`, `app/schemas/__init__.py`), the
  `alembic/env.py` traversal change, the explicit ingestion source
  assignment, and the PR2 test additions — all **before any accepted
  API-created null-file rows exist**. After API rows exist, downgrade
  refusal is expected behavior; retain readers and data instead. No
  commit was made.

## Remaining tasks (exact unchecked lines from tasks.md)

Sections 3.1–3.6 (PR3), 4.1–4.4 (PR4), 5.1–5.5 (PR5), and section 6
final bookkeeping remain unchecked. One PR2-adjacent note: the section
6.1 instruction to "update apply-progress.md after each PR/work unit" is
satisfied cumulatively by this file; its checkbox stays unchecked until
final verification.

## Structured status

- Change: `add-transaction-creation-api`; artifact store: openspec
  (Engram unavailable — evidence persisted here and in `tasks.md`
  checkbox updates only; no memory-tool persistence claimed).
- `actionContext`: repo-local, workspace root
  `/home/tadashi/orca/workspaces/finhealth/transactions-api`; all edits
  stayed inside the allowed edit roots and the declared PR2 surfaces
  (plus `app/api/v1/router.py`, restored byte-identical after a
  stale-cache mypy investigation — zero net change).
- Skill resolution: `paths-injected` (gentle-ai, work-unit-commits,
  cognitive-doc-design SKILL.md files read before work; no registry
  discovery, no child subagents spawned).
- Native SDD attempt token from the parent prompt: PR2 attempt recorded;
  persisted-task checkboxes for sections 2.1–2.3 marked `[x]` in
  `tasks.md` (re-read and confirmed).

## Status: PR 3 complete — Ready for verify (PR 3 slice)

PR 3 (nested metadata schemas and internal parent planning) is implemented
under strict TDD with PostgreSQL-backed evidence. PR1/PR2 files are
untouched (`app/services/merchants.py`, `tests/test_merchants.py`,
models, migrations, ingestion, routes all unmodified). PR4 (persistence +
routes) and PR5 are not started. No route persistence exists: the service
stops at an explicit, test-pinned `NotImplementedError` boundary after
planning succeeds, and `app/api/v1/transactions.py` has no POST handlers.

Delivery decision consumed from the parent prompt (recorded here per the
`ask-on-risk` gate): `feature-branch-chain`, PR3 slice assigned with a
declared 350-changed-line budget and a native attempt token.

## Changed lines (PR 3 slice)

| File | Additions | Deletions |
|---|---|---|
| `app/schemas/__init__.py` | 6 | 0 |
| `app/schemas/domain.py` | 125 | 4 |
| `app/services/transaction_creation.py` (new) | 331 | 0 |
| `tests/test_transaction_creation.py` (new) | 637 | 0 |
| **Authored total (additions + deletions)** | **1103** | |

Measured with `git diff --numstat` plus new-file line counts. **This
slice exceeds the declared 350-line budget (1103 authored lines).** The
overage is structural, not accidental: tasks 3.1–3.4 enumerate ~25
distinct behaviors (four required metadata fields, ten forbidden nested
fields, money float/bool/non-finite/precision/range rules, installment
INTEGER overflow, batch bounds and indexed locations, six planner rules
plus bounds/no-writes/boundary tests), and strict TDD requires one
failing test per behavior before implementation. **Budget-breach
decision referred to the parent** under `ask-on-risk`: either re-slice
PR3 into 3a (schemas + schema tests) and 3b (planner service + service
tests) as separate chain entries, or explicitly accept `size:exception`
before PR creation. No commit was made, so re-slicing before the PR
boundary is still possible. Coverage was not trimmed to force the budget
because every test maps 1:1 to an enumerated task behavior.

## TDD Cycle Evidence (PR 3)

Command prefix for every run below:

```sh
POSTGRES_USER=finhealth POSTGRES_PASSWORD=secret POSTGRES_DB=finhealth \
POSTGRES_TEST_HOST=127.0.0.1 POSTGRES_TEST_PORT=5432 \
POSTGRES_TEST_USER=finhealth POSTGRES_TEST_PASSWORD=secret pytest
```

### RED (tasks 3.1 + 3.3)

- Schema RED: `pytest tests/test_transaction_creation.py -q --no-cov` →
  collection `ImportError: cannot import name 'StatementMetadataCreate'
  from 'app.schemas'` (schema additions absent).
- Planner RED: after schemas landed, appending the service tests →
  collection `ImportError` on `app.services.transaction_creation`
  (module absent).

### GREEN (tasks 3.2 + 3.4)

- `pytest tests/test_transaction_creation.py -q --no-cov` → `62 passed`
  after the schema additions (metadata closure/period rules, extended
  `TransactionCreate` with `category_id`/`statement`/money guard/
  INTEGER bounds, bounded batch contracts and indexed locations).
- `pytest tests/test_transaction_creation.py -q --no-cov` → `79 passed`
  after the planning service (conflict/required/duplicate codes with
  field+index attribution, metadata-anywhere, mixed IDs, unknown-card
  404, lowest-index precedence, no-writes, persistence boundary).

### TRIANGULATE (task 3.4)

- Added multiple-new-parents (two new UUIDs, metadata on first and last
  items, distinct authoritative objects) and a USD-card plan (currency
  resolved from the card row, not assumed CLP).
- Parametrized boundaries already in the suite: metadata at first/middle/
  last index; existing-parent conflict with matching AND mismatching
  metadata; duplicate metadata identical AND conflicting; three
  lowest-index-wins orderings across 409/422 classes; plan-definition
  pass preceding card lookup (higher-index plan violation beats
  lower-index unknown-card 404, per the design's validation order).
- Result: `81 passed in 4.19s`.

### REFACTOR

- Merged the double `isinstance` in the money guard (SIM101), replaced a
  dict-comprehension that mypy rejected in `dict()` form with an indexed
  row projection (C416 vs SQLAlchemy `Result` typing), moved the service
  import into the top import block (drops `noqa: E402`), switched the
  test timestamp to `datetime.UTC` (UP017), and ran `ruff format`.
  No behavior change; suite re-run green (`81 passed`).

## Verification evidence (PR 3)

| Command | Result |
|---|---|
| `... pytest tests/test_transaction_creation.py -q --no-cov` | `81 passed in 4.35s` |
| `... pytest tests/test_transaction_creation.py tests/test_merchants.py tests/test_transactions.py tests/test_models.py tests/test_categories.py -q --no-cov` | `151 passed in 16.42s` |
| `... pytest tests/test_ingestion.py tests/test_web_phase1.py tests/test_health.py tests/test_db.py -q --no-cov` | `103 passed, 50 skipped in 18.08s` — skips are exclusively TEST_RUT real-PDF E2E prerequisites (unavailable, not passed) |
| `ruff check app tests` | All checks passed |
| `ruff format --check` (4 touched files) | All formatted |
| `mypy --strict app/services/transaction_creation.py app/schemas/domain.py app/schemas/__init__.py` (clean cache) | 0 errors in these files; full `mypy --strict app/` shows the 6 pre-existing baseline errors elsewhere (dashboard, seed_demo, llm client, web/router ×3) and none in PR3 files |

Runtime harness scenario: PostgreSQL-backed disposable databases via the
`engine` fixture for every planner test (planning reads real persisted
parents/cards); schema tests are pure Pydantic. No skipped database test
was used as evidence.

## Implementation notes and deviations from design

- The planner implements the design's two-phase validation order: the
  parent-definition pass (409 existing+metadata / 422 missing / 422
  duplicate, lowest offending index) runs before card resolution
  (unknown new-parent card → 404 at its metadata index). A test pins
  this precedence.
- `plan_parents` is an internal seam the caller must invoke inside an
  active transaction; `create_many` owns the single outer transaction
  (`session.begin()` before the first read) and re-enforces the 1–200
  batch bounds before opening it (`invalid_batch`).
- Two defensive `creation_failed` branches exist for parent-linkage
  integrity (persisted parent without card row; new plan without
  metadata/card after validation). Both are unreachable through
  FK-consistent data (the statement→card FK cannot be violated in a
  seeded test), so they are untestable by construction; the design maps
  invalid parent linkage to a generic 500 `creation_failed`, which is
  what these branches raise. Recorded here rather than silently omitted.
- `TransactionCreate` gained the creation-only money guard and INTEGER
  `le` bounds directly (it is the creation schema and currently unused
  by any route); `TransactionResponse` and PDF-specific
  `StatementCreate` are unchanged, per tasks 3.2.
- Metadata objects are compared never: an existing parent plus any
  non-null metadata object conflicts regardless of values, so "matching"
  is only exercised as identical-to-persisted input in tests.

- Task 3.4's batch-fetch bullet lists "statements/cards/categories";
  planning needs only statements and cards, so this slice fetches those.
  The category set is fetched once each for per-item category/currency
  validation in PR4 (design steps 2 and 5), which is where unknown
  category 404s are specified (tasks 4.1).

## Workload / PR boundary

- PR boundary: creation-only schemas + internal parent-planning service
  skeleton and their tests. No models, migrations, ingestion, merchants,
  routes, docs, or PR1/PR2 behavior changes. The service performs reads
  only and never persists; routes are absent.
- Changed-line count: 1103 authored (additions + deletions) — **exceeds
  the 350-line slice budget**; parent decision required before PR
  creation (see Changed lines section).
- Rollback boundary: delete `app/services/transaction_creation.py`,
  `tests/test_transaction_creation.py`, and revert the schema additions
  in `app/schemas/domain.py` (new `StatementMetadataCreate`,
  `TransactionBatchCreate`, `TransactionBatchResponse`, `category_id`/
  `statement` fields, money guard, installment `le` bounds) and the six
  export lines in `app/schemas/__init__.py`. PR2 statement
  compatibility (nullable files, source) remains intact; no data or
  migration state is affected because this slice writes nothing. No
  commit was made.

## Remaining tasks (exact unchecked lines from tasks.md)

Sections 4.1–4.4 (PR4), 5.1–5.5 (PR5), and section 6 final bookkeeping
remain unchecked, plus the three scope guardrails at the top of the file
(checked at final verification). PR3 sections 3.1–3.4 are fully checked
(16 boxes re-read and confirmed `[x]`).

## Structured status

- Change: `add-transaction-creation-api`; artifact store: openspec
  (Engram unavailable — evidence persisted here and in `tasks.md`
  checkbox updates only; no memory-tool persistence claimed).
- `actionContext`: repo-local, workspace root
  `/home/tadashi/orca/workspaces/finhealth/transactions-api`; all edits
  stayed inside the allowed edit roots and the declared PR3 surfaces
  (`app/schemas/domain.py`, `app/schemas/__init__.py`, new
  `app/services/transaction_creation.py`, new
  `tests/test_transaction_creation.py`, PR3 sections of `tasks.md` and
  this file).
- Skill resolution: `paths-injected` (gentle-ai, work-unit-commits,
  cognitive-doc-design SKILL.md files read before work; no registry
  discovery, no child subagents spawned).
- Native SDD attempt token from the parent prompt: PR3 attempt
  `sha256:b6890e9494ae3ccb6f047a4be15f7abc1836bbb1b2d1c30e927625946d7a12fb`
  recorded; persisted-task checkboxes for sections 3.1–3.4 marked `[x]`
  in `tasks.md` (re-read and confirmed).

## Status: PR 4 complete — Ready for verify (PR 4 slice)

PR 4 (atomic statement+transaction creation and public routes) is
implemented under strict TDD with PostgreSQL-backed evidence. PR1–PR3
files are untouched (`app/services/merchants.py`, models, migrations,
ingestion, schemas, statement routes all unmodified). PR5 (race/rollback
hardening, docs, full verification) is not started. The service's
`NotImplementedError` persistence boundary is replaced by the full atomic
write path, and `POST /api/v1/transactions` + `POST /api/v1/transactions/batch`
are mounted in the existing transactions router (no `router.py` change —
the transactions router was already registered).

Delivery decision consumed from the parent prompt (recorded here per the
`ask-on-risk` gate): `feature-branch-chain`, PR4 slice assigned with a
declared 350-changed-line budget and a native attempt token; **the
maintainer explicitly accepts `size:exception` for oversized
implementation PRs**, so the budget overage below is authorized.

## Changed lines (PR 4 slice)

| File | Additions | Deletions |
|---|---|---|
| `app/api/v1/transactions.py` | 160 | 10 |
| `app/services/transaction_creation.py` | 320 | 32 |
| `tests/test_transaction_creation.py` | 519 | 24 |
| `tests/test_transaction_creation_http.py` (new) | 526 | 0 |
| **Authored total (additions + deletions)** | **1591** | |

Measured with `git diff --numstat` plus the new-file line count.
**This slice exceeds the declared 350-line budget (1591 authored
lines).** The overage is structural: tasks 4.1–4.4 enumerate ~25
distinct persistence/policy/HTTP behaviors and strict TDD requires one
failing test per behavior; the maintainer's `size:exception` acceptance
(from the parent prompt) covers this slice. No commit was made.

## TDD Cycle Evidence (PR 4)

Command prefix for every run below:

```sh
POSTGRES_USER=finhealth POSTGRES_PASSWORD=secret POSTGRES_DB=finhealth \
POSTGRES_TEST_HOST=127.0.0.1 POSTGRES_TEST_PORT=5432 \
POSTGRES_TEST_USER=finhealth POSTGRES_TEST_PASSWORD=secret pytest
```

### RED (tasks 4.1 + 4.3)

Command: `... pytest tests/test_transaction_creation.py
tests/test_transaction_creation_http.py -q --no-cov`
Result: `45 failed, 81 passed in 14.35s`

* Service persistence RED: all 27 new `TestAtomicPersistence` tests
  failed with `NotImplementedError: transaction persistence lands in
  the next chain slice (PR4)` at the PR3 boundary — the planning half
  passed, no write path existed.
* HTTP RED: all 18 new route tests failed with `405 Method Not
  Allowed` — no POST handlers existed.
* The 81 passing tests are the pre-existing PR3 schema/planner suite,
  confirming no regression at the RED stage.

### GREEN (tasks 4.2 + 4.3)

Change: completed `TransactionCreationService.create_many` on top of the
same planning pass — `_fetch_categories` (one query, keyed by ID and
lowercase name), `_validate_items` (currency + explicit category in
input order, first failure raises with its index), `_create_new_statements`
(canonical UUID order, individually flushed, `source=api`,
`status=completed`, null files/error, supplied UUID/card/dates; only
statement-PK `23505`+`pk_statements` maps to
`statement_creation_conflict`, all other DB failures abort generically),
`_build_transaction_rows` (merchant resolution for all items, rows added
and flushed once after enrichment, explicit field allowlist, category
precedence table, at-most-one deprecation warning per request,
merchant length-bounds guard, no LLM path), and `_snapshot_rows`
(one re-select populates SQL-expression defaults; response snapshots
built before commit, returned only after the outer context exits).
Routes: thin POST handlers in `app/api/v1/transactions.py` delegating to
`TransactionCreationService.create_many([payload])` /
`(payload.transactions)` with a single code→status mapping table
(`{"detail": {"code", "message", "field"?, "index"?}}`; single-route
mapping omits index) and JSONResponse bodies via `model_dump(mode="json")`
(matching the statements-upload response convention for Decimal
encoding). One service path serves single and batch.

* `pytest tests/test_transaction_creation.py -q --no-cov` → `105 passed`
  (after green; included the 2 obsolete-assert fixes in new tests).
* `pytest tests/test_transaction_creation_http.py -q --no-cov` →
  `20 passed`.

### TRIANGULATE (tasks 4.4)

* Batch 200 success with accurate count; 0/201 rejected 422 with zero
  writes (HTTP).
* Duplicate-looking identical items in one batch and repeated identical
  submissions create distinct rows (HTTP: 3 identical items → 3 distinct
  IDs; +1 repeat submission → 4 rows).
* API append to an existing PDF statement leaves `source=pdf` and file
  fields intact (HTTP).
* Concurrent parent commit between the planning read and the parent
  insert (hooked `_fetch_statement_cards` barrier): loser receives 409
  `statement_creation_conflict` (field `statement_id`, index 0) with
  zero request-owned writes, winner intact, and an explicit ID-only
  retry succeeds (service).
* Category precedence parametrized with/without legacy string; currency
  policy parametrized across CLP/USD/`EUR`/lowercase `clp`/mismatch in
  both directions; long-description guard parametrized at raw 201 and
  canonical 109 (`MCDONALDS SUC 12 ` × 11); deprecation warning logged
  exactly once for two legacy items and never for ID-only input.
* Regression suites: `pytest tests/test_transactions.py
  tests/test_merchants.py tests/test_categories.py tests/test_models.py
  tests/test_ingestion.py -q --no-cov` → `116 passed, 50 skipped`
  (skips are exclusively TEST_RUT real-PDF E2E prerequisites —
  unavailable, not passed). GET filters, PATCH form/HTML/JSON, PATCH
  clear sentinel, category, merchant and ingestion behavior unchanged.

### REFACTOR

* Removed the obsolete PR3 boundary pin
  (`test_create_many_stops_at_persistence_boundary`) — superseded by the
  completed persistence path.
* Fixed the `_fetch_cards` eager-load hazard discovered by the
  concurrent-conflict test: `select(CreditCard)` fired `lazy="selectin"`
  on `statements` and pulled a concurrently committed winner into the
  session identity map, emitting a SQLAlchemy identity-conflict warning
  before the PK race resolved. The card fetch now uses
  `noload(CreditCard.bank)` / `noload(CreditCard.statements)`; the race
  surfaces as the database's own unique violation, mapped cleanly to
  409. Suite re-run with `-W error::sqlalchemy.exc.SAWarning` →
  `125 passed, 0 warnings`.
* Dropped unused variables/imports, renamed the discriminator's fake
  diagnostic class (N818), and ran `ruff format` on all four touched
  files. No behavior change; suite re-run green (`125 passed`).

## Verification evidence (PR 4)

| Command | Result |
|---|---|
| `... pytest tests/test_transaction_creation.py tests/test_transaction_creation_http.py -q --no-cov` | `125 passed in 15.65s` (final, with `-W error::sqlalchemy.exc.SAWarning`) |
| `... pytest tests/test_transactions.py tests/test_merchants.py tests/test_categories.py tests/test_models.py tests/test_ingestion.py -q --no-cov` | `116 passed, 50 skipped in 17.50s` — skips are exclusively TEST_RUT/sample-PDF E2E prerequisites (unavailable, not passed) |
| `ruff check app tests` | All checks passed |
| `ruff format --check` (4 touched files) | All formatted |
| `mypy --strict app/` (clean cache) | 6 errors — all pre-existing baseline (dashboard, seed_demo, llm client, web/router ×3); 0 in PR4 files |

Runtime harness scenario: real PostgreSQL 16 disposable databases
created and dropped per test (`tests/conftest.py`); every
persistence/atomicity/conflict assertion ran against real PostgreSQL, and
durable-state assertions use independent sessions. No skipped database
test was used as evidence. Real-PDF E2E remains environment-dependent
(`TEST_RUT` + sample PDFs) — recorded as unavailable.

## Implementation notes and deviations from design

- Response serialization follows the statements-upload route's existing
  convention (`JSONResponse` + `model_dump(mode="json")`), so Decimal
  amounts serialize as strings in creation responses. Existing GET/PATCH
  responses are untouched (their Decimal encoding is unchanged).
- The single route omits `index` from error envelopes per the design's
  error-mapping contract ("single-route mapping omits index"); batch
  domain errors keep the zero-based index.
- `_snapshot_rows` re-selects the flushed rows in one query so
  `created_at`/`updated_at` (SQL-expression defaults) populate without a
  per-row refresh, inside the transaction before commit — matching the
  design's "any necessary refresh happens before commit" rule.
- Merchant-binding eligibility is guarded by the design's length bounds
  (raw > 200 or canonical > 100 skips the resolver, merchant NULL,
  description preserved verbatim); alias hits for oversized descriptions
  are intentionally skipped per the design's conservative choice.
- The concurrent-parent test hooks `_fetch_statement_cards` (the
  planner's first read) with an asyncio barrier to make the
  winner-commits-between-read-and-insert race deterministic in one event
  loop; the real two-session barrier matrix remains PR5 scope (5.2).
- `test_planning_failure_writes_nothing` (PR3) and the new
  `test_unknown_category_not_found_without_writes` /
  currency/cleanup assertions together cover "no writes on domain
  failures"; late-flush/commit-time failure injection remains PR5 (5.1).

## Workload / PR boundary

- PR boundary: atomic creation service persistence + the two public
  POST routes and their tests. No models, migrations, ingestion,
  merchants, schemas, statement routes, docs, or PR1–PR3 behavior
  changes. `app/api/v1/router.py` unchanged (the POST routes are
  registered on the already-mounted transactions router).
- Changed-line count: 1591 authored (additions + deletions) — exceeds
  the 350-line slice budget; covered by the maintainer's explicit
  `size:exception` acceptance recorded in the parent prompt.
- Rollback boundary: remove the two POST handlers and the
  error-mapping helper from `app/api/v1/transactions.py`, revert the
  persistence additions in `app/services/transaction_creation.py`
  (restoring the PR3 planning-only skeleton), delete
  `tests/test_transaction_creation_http.py`, and revert the PR4 test
  additions in `tests/test_transaction_creation.py`. PR2 statement
  compatibility (nullable files, source) and PR1 merchant savepoints
  remain intact; any accepted API-created rows keep their `api`
  provenance — never reclassify or delete them. No commit was made.

## Remaining tasks (exact unchecked lines from tasks.md)

Sections 5.1–5.5 (PR5) and section 6 final bookkeeping remain unchecked,
plus the three scope guardrails at the top of the file (checked at final
verification). PR4 sections 4.1–4.4 are fully checked (17 boxes re-read
and confirmed `[x]`).

## Structured status

- Change: `add-transaction-creation-api`; artifact store: openspec
  (Engram unavailable — evidence persisted here and in `tasks.md`
  checkbox updates only; no memory-tool persistence claimed).
- `actionContext`: repo-local, workspace root
  `/home/tadashi/orca/workspaces/finhealth/transactions-api`; all edits
  stayed inside the allowed edit roots and the declared PR4 surfaces
  (`app/services/transaction_creation.py`,
  `app/api/v1/transactions.py`, `tests/test_transaction_creation.py`,
  new `tests/test_transaction_creation_http.py`, PR4 sections of
  `tasks.md` and this file). `router.py` registration not required.
- Skill resolution: `paths-injected` (gentle-ai, work-unit-commits,
  cognitive-doc-design SKILL.md files read before work; no registry
  discovery, no child subagents spawned).
- Native SDD attempt token from the parent prompt: PR4 attempt
  `sha256:e1e0e8818d4a8dda7fffbe12fdedcf0b0f92d0d5e9bf1f7a8ba4435d73bd1216`
  recorded; persisted-task checkboxes for sections 4.1–4.4 marked `[x]`
  in `tasks.md` (re-read and confirmed).

## Status: PR 5 slice A complete — request-wide rollback hardening tests

PR 5 slice A adds PostgreSQL failure-injection coverage for the existing
transaction creation service's request-wide rollback boundary. The service code
was inspected through the new tests and did not require a behavior change: the
PR4 `async with session.begin()` boundary already rolls back request-created
statements, transactions, merchants, and aliases for the injected failure points.
No concurrency, docs, PDF tests, unrelated routes, or PR5 sections beyond 5.1
were implemented in this slice.

Delivery decision consumed from the parent prompt: PR5 slice A only, with
`size:exception` accepted for the chain and native attempt token
`sha256:e5d30373ece278596add367f3c562aaf275c351704923e815df611d15ba01a81`.

## Changed lines (PR 5 slice A)

| File | Change |
|---|---|
| `tests/test_transaction_creation.py` | Added fresh-session rollback assertion helper and five PostgreSQL failure-injection tests: after new statement flush, after merchant/alias writes, during transaction flush, during response snapshot validation, and before commit. |
| `openspec/changes/add-transaction-creation-api/tasks.md` | Marked the completed PR5 5.1 slice-A checkboxes and left the broader PR5 5.1 concurrency/late-invalid/recovered-conflict checkbox unchecked. |
| `openspec/changes/add-transaction-creation-api/apply-progress.md` | Recorded this evidence. |

## TDD Cycle Evidence (PR 5 slice A)

Command prefix:

```sh
POSTGRES_USER=finhealth POSTGRES_PASSWORD=secret POSTGRES_DB=finhealth \
POSTGRES_TEST_HOST=127.0.0.1 POSTGRES_TEST_PORT=5432 \
POSTGRES_TEST_USER=finhealth POSTGRES_TEST_PASSWORD=secret pytest
```

### RED (task 5.1 slice A)

Strict RED did not produce an observed failure: after adding the five
failure-injection tests, the current PR4 service passed them immediately.
Observed command: `... pytest tests/test_transaction_creation.py::TestAtomicPersistence -q --no-cov` →
`30 passed in 6.89s` before formatting, then `30 passed in 7.02s` after formatting.

This is recorded as a strict-TDD exception for a hardening/characterization slice:
no service implementation change was needed to make the new behavior-level tests
pass, so no failing RED can be claimed.

### GREEN (task 5.1 slice A)

No service code changed. The same focused PostgreSQL command is the GREEN evidence:
`... pytest tests/test_transaction_creation.py::TestAtomicPersistence -q --no-cov` →
`30 passed in 7.02s`.

### TRIANGULATE

The new tests cover five distinct abort points and each verifies durable state
from a fresh independent session using request-owned statement IDs/descriptions:

- after an API statement is flushed, before merchant/transaction work;
- after deterministic merchant and raw alias writes, before the second item completes;
- during the transaction flush after parent and merchant/alias writes;
- during `TransactionResponse` snapshot validation after transaction rows flush;
- immediately before commit via a SQLAlchemy `before_commit` hook.

Each assertion confirms no request-created statement, transaction, merchant, or
alias rows survived, while the pre-existing seeded parent statement remains.

### REFACTOR

No service refactor. Test helper extraction only (`_assert_no_request_created_rows`)
so each failure-injection test uses the same fresh-session durable-state check.

## Verification evidence (PR 5 slice A)

| Command | Result |
|---|---|
| `POSTGRES_USER=finhealth POSTGRES_PASSWORD=secret POSTGRES_DB=finhealth POSTGRES_TEST_HOST=127.0.0.1 POSTGRES_TEST_PORT=5432 POSTGRES_TEST_USER=finhealth POSTGRES_TEST_PASSWORD=secret pytest tests/test_transaction_creation.py::TestAtomicPersistence -q --no-cov` | `30 passed in 7.02s` |
| `POSTGRES_USER=finhealth POSTGRES_PASSWORD=secret POSTGRES_DB=finhealth POSTGRES_TEST_HOST=127.0.0.1 POSTGRES_TEST_PORT=5432 POSTGRES_TEST_USER=finhealth POSTGRES_TEST_PASSWORD=secret pytest tests/test_transaction_creation.py -q --no-cov` | `110 passed in 10.42s` |
| `ruff check tests/test_transaction_creation.py` | All checks passed |
| `ruff format --check tests/test_transaction_creation.py` | 1 file already formatted |

Runtime harness scenario: disposable PostgreSQL databases from the existing
`engine` fixture; rollback durability verified through fresh independent sessions.
No skipped database test was used as evidence.

## Workload / PR boundary

- PR boundary: failure-injection tests for request-wide rollback only. No service
  behavior change, no routes, no docs/README, no PDF regression tests, and no
  PR5 concurrent race work.
- Rollback boundary: revert the PR5 additions in `tests/test_transaction_creation.py`
  plus the PR5 5.1 evidence/checkbox edits in `tasks.md` and this file. PR1–PR4
  implementation remains intact.
- Remaining PR5 work: the broader 5.1 second checkbox (late invalid batch items,
  multiple flushed new parents, recovered merchant conflicts followed by later
  failure, and existing/concurrent parent conflicts), 5.2 concurrency race matrix,
  5.3 PDF/migration regressions, 5.4 documentation, 5.5 final verification, and
  section 6 bookkeeping.

## Structured status

- Change: `add-transaction-creation-api`; artifact store: openspec.
- `actionContext`: repo-local, workspace root
  `/home/tadashi/orca/workspaces/finhealth/transactions-api`; all edits stayed
  inside the allowed edit surfaces for PR5 slice A.
- Skill resolution: `paths-injected` (gentle-ai and work-unit-commits SKILL.md
  files read before work; no registry discovery, no child subagents spawned).

## Status: PR 5 slice B complete — true new-statement race hardening tests

PR 5 slice B adds PostgreSQL two-session/barrier coverage for concurrent creation
of the same missing `statement_id`. Both service invocations use independent
sessions, both read the parent set before either insert is released, and the real
statement primary key decides the race. No service behavior change was required:
the PR4 parent-flush conflict mapping already returns `statement_creation_conflict`
and the outer transaction rolls back loser-owned rows.

No docs, PDF, migration, final verification, route rewrites, or PR5 tasks outside
this narrow 5.2 race slice are claimed complete.

Delivery decision consumed from the parent prompt: PR5 slice B only, with
`size:exception` accepted for the chain and native attempt token
`sha256:e5d30373ece278596add367f3c562aaf275c351704923e815df611d15ba01a81`.

## Changed lines (PR 5 slice B)

| File | Change |
|---|---|
| `tests/test_transaction_creation.py` | Added `_description_count` and a parametrized two-session/barrier race test for matching and differing valid metadata. The test asserts both sessions observed absence, exactly one request succeeded, the loser received `statement_creation_conflict` 409-equivalent service error, loser-owned transaction rows were absent before retry, explicit ID-only retry succeeded, and resending metadata after success remained 409. |
| `openspec/changes/add-transaction-creation-api/tasks.md` | Marked the first PR5 5.2 concurrency checkbox complete only; broader 5.2 extras, PDF/docs/final verification remain unchecked. |
| `openspec/changes/add-transaction-creation-api/apply-progress.md` | Recorded this slice-B evidence. |

## TDD Cycle Evidence (PR 5 slice B)

Command prefix:

```sh
POSTGRES_USER=finhealth POSTGRES_PASSWORD=secret POSTGRES_DB=finhealth \
POSTGRES_TEST_HOST=127.0.0.1 POSTGRES_TEST_PORT=5432 \
POSTGRES_TEST_USER=finhealth POSTGRES_TEST_PASSWORD=secret pytest
```

### RED (task 5.2 slice B)

Strict RED did not produce an observed failure: after adding the two-session
barrier race test, the current service passed both matching and differing metadata
cases immediately.

Observed command:
`... pytest tests/test_transaction_creation.py::TestAtomicPersistence::test_two_sessions_racing_same_new_statement_conflict_atomically -q --no-cov` →
`2 passed in 0.62s`.

This is recorded as a strict-TDD exception for a hardening/characterization slice:
no implementation defect was exposed, so no failing RED can be claimed.

### GREEN (task 5.2 slice B)

No service code changed. Focused PostgreSQL command after formatting:
`... pytest tests/test_transaction_creation.py::TestAtomicPersistence -q --no-cov` →
`32 passed in 7.75s`.

### TRIANGULATE

The parametrized race covers two metadata cases for the same new statement UUID:

- matching valid metadata in both requests;
- differing but independently valid metadata (`statement_date` differs).

For both cases, the test proves both independent sessions observed `{}` from the
parent lookup before inserts were released, exactly one request committed the
statement+transaction, the loser raised `statement_creation_conflict` with field
`statement_id` and index `0`, the loser description had zero durable transactions
before retry, the winner row remained durable, an explicit ID-only loser retry
succeeded, and resending metadata after success returned `statement_already_exists`.

### REFACTOR

No service refactor. Test-only helper extraction (`_description_count`) keeps the
race assertions scoped to exact loser/winner descriptions.

## Verification evidence (PR 5 slice B)

| Command | Result |
|---|---|
| `POSTGRES_USER=finhealth POSTGRES_PASSWORD=secret POSTGRES_DB=finhealth POSTGRES_TEST_HOST=127.0.0.1 POSTGRES_TEST_PORT=5432 POSTGRES_TEST_USER=finhealth POSTGRES_TEST_PASSWORD=secret pytest tests/test_transaction_creation.py::TestAtomicPersistence::test_two_sessions_racing_same_new_statement_conflict_atomically -q --no-cov` | `2 passed in 0.62s` |
| `POSTGRES_USER=finhealth POSTGRES_PASSWORD=secret POSTGRES_DB=finhealth POSTGRES_TEST_HOST=127.0.0.1 POSTGRES_TEST_PORT=5432 POSTGRES_TEST_USER=finhealth POSTGRES_TEST_PASSWORD=secret pytest tests/test_transaction_creation.py::TestAtomicPersistence -q --no-cov` | `32 passed in 7.75s` |
| `POSTGRES_USER=finhealth POSTGRES_PASSWORD=secret POSTGRES_DB=finhealth POSTGRES_TEST_HOST=127.0.0.1 POSTGRES_TEST_PORT=5432 POSTGRES_TEST_USER=finhealth POSTGRES_TEST_PASSWORD=secret pytest tests/test_transaction_creation.py -q --no-cov` | `112 passed in 11.03s` |
| `ruff check tests/test_transaction_creation.py` | All checks passed |
| `ruff format --check tests/test_transaction_creation.py` | 1 file already formatted |

Runtime harness scenario: disposable PostgreSQL databases from the existing
`engine` fixture; two independent `AsyncSession` instances synchronized by an
async barrier after the absence read and before parent insert. No skipped database
test was used as race evidence.

## Workload / PR boundary

- PR boundary: true service-level new-parent race tests only. No service behavior
  change, no HTTP route changes, no docs/README, no PDF/migration regression tests,
  and no final verification.
- Rollback boundary: revert the PR5 slice-B additions in
  `tests/test_transaction_creation.py` plus the PR5 5.2 evidence/checkbox edits in
  `tasks.md` and this file. PR1–PR5 slice A implementation remains intact.
- Remaining PR5 work: the broader 5.1 second checkbox, 5.2 extras not claimed by
  this slice (reversed multi-parent order, earlier inserted loser parents, winner
  rollback allowing the other insert to succeed, no automatic retry/reuse beyond
  the explicit retry assertion, and non-PK error classification beyond the existing
  discriminator), 5.3 PDF/migration regressions, 5.4 documentation, 5.5 final
  verification, and section 6 bookkeeping.

## Structured status

- Change: `add-transaction-creation-api`; artifact store: openspec.
- `actionContext`: repo-local, workspace root
  `/home/tadashi/orca/workspaces/finhealth/transactions-api`; all edits stayed
  inside the allowed edit surfaces for PR5 slice B.
- Skill resolution: `paths-injected` (gentle-ai and work-unit-commits SKILL.md
  files read before work; no registry discovery, no child subagents spawned).

## Status: PR 5 slice C complete — docs and final PR5 compatibility regressions

PR 5 slice C completes the remaining PR5 regression/documentation work without
marking the final verify/archive tasks complete. It adds two PostgreSQL race
triangulation tests for the parent-creation edge cases left after slices A/B,
documents the transaction creation API contract in `README.md`, and re-runs the
focused compatibility suites for transaction creation, migrations/source
separation, models, and PDF ingestion. No application source code changed.

Delivery decision consumed from the parent prompt: PR5 slice C only, with
`size:exception` accepted for the chain and native attempt token
`sha256:e5d30373ece278596add367f3c562aaf275c351704923e815df611d15ba01a81`.

## Changed lines (PR 5 slice C)

| File | Change |
|---|---|
| `tests/test_transaction_creation.py` | Added PR5 race triangulation for reversed multi-parent input where an earlier flushed loser-owned parent is rolled back after a later contested parent conflict, plus a winner-rollback case where a waiting request succeeds instead of receiving a false 409. |
| `README.md` | Added the transaction creation API contract: existing-parent ID-only usage, new-parent nested metadata, mixed batch/shared metadata, 409 ID-only retry, nullable statement response/source, API completed-status meaning, category/merchant/currency rules, 1-200 bounds, non-idempotent timeout caveats, and explicit non-goals. |
| `openspec/changes/add-transaction-creation-api/tasks.md` | Marked PR5 5.1 remaining regression coverage, 5.2 race extras/discriminator evidence, 5.3 compatibility regressions, and 5.4 docs complete; final verification/archive tasks remain unchecked. |
| `openspec/changes/add-transaction-creation-api/apply-progress.md` | Recorded this slice-C evidence. |

## TDD Cycle Evidence (PR 5 slice C)

Command prefix for PostgreSQL-backed pytest runs:

```sh
POSTGRES_USER=finhealth POSTGRES_PASSWORD=secret POSTGRES_DB=finhealth \
POSTGRES_TEST_HOST=127.0.0.1 POSTGRES_TEST_PORT=5432 \
POSTGRES_TEST_USER=finhealth POSTGRES_TEST_PASSWORD=secret pytest
```

### RED (tasks 5.1, 5.2, 5.4)

Strict RED did not produce an observed failure for the two new race tests: they are
hardening/characterization coverage of behavior already implemented by PR4 and
partly proved by slices A/B. After adding the tests, the current service passed the
focused class immediately.

Observed command:
`... pytest tests/test_transaction_creation.py::TestAtomicPersistence -q --no-cov` →
`34 passed in 8.37s`.

Manual documentation RED checklist before editing `README.md`: the existing README
had no transaction-creation section documenting existing-parent ID-only examples,
new-parent nested metadata, mixed batch/shared metadata, 409 ID-only retry,
nullable statement response/source, API completed-status meaning,
category/currency/merchant behavior, 1-200 bounds, append-only/non-idempotent/
timeout duplicate caveats, or explicit non-goals.

### GREEN (tasks 5.1, 5.2, 5.4)

No service code changed. README now contains the required API contract and examples.
Focused PostgreSQL command after `ruff format`:
`... pytest tests/test_transaction_creation.py::TestAtomicPersistence -q --no-cov` →
`34 passed in 12.30s`.

### TRIANGULATE (task 5.3)

- Reversed multi-parent/input-order conflict: request input lists the contested
  parent before another new parent, but canonical UUID insert order flushes the
  earlier UUID first; a concurrently committed contested winner causes
  `statement_creation_conflict`, and the earlier loser-owned parent is absent in a
  fresh session.
- Winner rollback: first inserter flushes the contested parent then raises before
  commit; the waiting request then creates the parent successfully, proving rollback
  permits the next insert rather than being mapped as a race conflict.
- Existing slice-B tests still cover matching and differing concurrent metadata,
  loser atomic 409, no loser-owned transaction rows, explicit ID-only retry success,
  and resending metadata after success returning `statement_already_exists`.
- `test_statement_pk_discriminator` covers exact `pk_statements`/SQLSTATE 23505
  classification and rejects card/hash uniqueness and FK/non-unique/non-diagnostic
  failures as parent races.
- Focused regression command:
  `... pytest tests/test_transaction_creation.py tests/test_transaction_creation_http.py tests/test_alembic.py tests/test_models.py tests/test_ingestion.py -q --no-cov` →
  `206 passed, 50 skipped in 44.85s`.
  The skipped tests are the real-PDF E2E/upload pipeline tests gated on
  `TEST_RUT` (sample PDF prerequisites are present enough for collection, but the
  RUT is unavailable). These skips are recorded as unavailable, not passed. The
  ungated portions rerun route/source/API separation, migration upgrade/downgrade,
  nullable/source, unsafe downgrade, uniqueness, model, and ingestion regressions.
- `./scripts/verify.sh` → focused dashboard/config/docs/docker checks passed:
  first pytest phase `30 passed, 89 skipped` (skips require `POSTGRES_TEST_HOST`,
  because the script does not export PostgreSQL test settings), Ruff passed, docs
  phase `8 passed`. The script completed with exit code 0.

### REFACTOR

No production refactor and no duplicated helpers removed. Test-only additions reuse
existing helpers where possible; `ruff format tests/test_transaction_creation.py`
reformatted the touched test file, then the focused class was re-run green.

## Verification evidence (PR 5 slice C)

| Command | Result |
|---|---|
| `POSTGRES_USER=finhealth POSTGRES_PASSWORD=secret POSTGRES_DB=finhealth POSTGRES_TEST_HOST=127.0.0.1 POSTGRES_TEST_PORT=5432 POSTGRES_TEST_USER=finhealth POSTGRES_TEST_PASSWORD=secret pytest tests/test_transaction_creation.py::TestAtomicPersistence -q --no-cov` | `34 passed in 12.30s` |
| `POSTGRES_USER=finhealth POSTGRES_PASSWORD=secret POSTGRES_DB=finhealth POSTGRES_TEST_HOST=127.0.0.1 POSTGRES_TEST_PORT=5432 POSTGRES_TEST_USER=finhealth POSTGRES_TEST_PASSWORD=secret pytest tests/test_transaction_creation.py tests/test_transaction_creation_http.py tests/test_alembic.py tests/test_models.py tests/test_ingestion.py -q --no-cov` | `206 passed, 50 skipped in 44.85s`; real-PDF E2E prerequisites unavailable because `TEST_RUT` is unset, recorded as unavailable rather than passed |
| `ruff check tests/test_transaction_creation.py && ruff format --check tests/test_transaction_creation.py` | All checks passed; 1 file already formatted |
| `./scripts/verify.sh` | Exit 0; first pytest phase `30 passed, 89 skipped`, Ruff passed, docs/docker phase `8 passed`; skips require `POSTGRES_TEST_HOST` because the script does not export PostgreSQL test settings |

Runtime harness scenario: disposable PostgreSQL databases from the existing
fixtures for transaction-creation/migration/model/ingestion tests. No skipped
database test was used as migration/race/atomicity evidence. Real-PDF E2E remains
environment-dependent (`TEST_RUT` and local sample PDFs); unavailable prerequisites
are recorded explicitly.

## Workload / PR boundary

- PR boundary: PR5 remaining race/regression/docs slice only. No application source
  code changed. Final full-suite/typecheck verification and verify-report/archive
  bookkeeping remain unchecked by instruction.
- Rollback boundary: remove the slice-C additions in
  `tests/test_transaction_creation.py`, remove the transaction-creation API section
  and POST endpoint rows from `README.md`, and revert the slice-C checkbox/evidence
  edits in `tasks.md` and this file. PR1-PR5 slice A/B implementation and evidence
  remain intact.

## Remaining tasks (exact unchecked lines from tasks.md)

Section 5.5 final verification remains unchecked by instruction, as do section 6
final apply/verify bookkeeping and archive-oriented confirmation tasks. The scope
guardrails at the top also remain unchecked until final verification.

## Structured status

- Change: `add-transaction-creation-api`; artifact store: openspec.
- `actionContext`: repo-local, workspace root
  `/home/tadashi/orca/workspaces/finhealth/transactions-api`; all edits stayed
  inside the allowed edit surfaces for PR5 slice C.
- Skill resolution: `paths-injected` (gentle-ai, cognitive-doc-design, and
  work-unit-commits SKILL.md files read before work; no registry discovery, no
  child subagents spawned).
