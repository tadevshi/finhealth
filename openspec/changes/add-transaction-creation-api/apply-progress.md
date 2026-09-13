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
