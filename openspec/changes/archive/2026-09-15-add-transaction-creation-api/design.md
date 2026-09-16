# Technical design: Atomic statement-linked transaction creation

## Decision and scope

Add JSON single and batch creation through the existing transactions router, backed by one creation service and one request-wide PostgreSQL transaction. Every item requires `statement_id`: link an existing parent by ID only, or explicitly create a missing parent under that UUID from nested metadata. New parents have null files, `source=api`, `status=completed` and no ingestion job. Currency comes from the resolved parent's card. Statements, merchants, aliases and transactions commit together or not at all.

Inputs: revised [proposal.md](proposal.md), historical [exploration.md](exploration.md), [transactions specification](specs/transactions-api/spec.md), [tasks.md](tasks.md), [apply-progress.md](apply-progress.md), `openspec/config.yaml` and current code. The confirmed proposal supersedes the exploration. The current spec/tasks still forbid parent creation and migrations, require missing-parent 404, and defer essential atomicity coverage; they MUST be aligned before further apply. The spec also inconsistently assigns unsupported currency 422; retain the proposal's 400 for well-formed business-rule failures. Do not repair legacy artifacts in this phase.

Scope is this repository's `app/` and Alembic architecture, not `packages/coding-agent`. Model/migration/reader compatibility and explicit PDF source assignment are required. PDF upload, file saving, hashing, deduplication and lifecycle remain otherwise unchanged; stopping PDF storage is deferred. No new router mount, UI, authorization model, reconciliation, transaction-origin migration or recurring trigger. This phase writes only this document; existing PR1 source work is neither edited nor re-verified.

## Architecture and contracts

```text
POST /api/v1/transactions          POST /api/v1/transactions/batch
          | TransactionCreate              | TransactionBatchCreate
          +-------------------+------------+
                              v
            TransactionCreationService.create_many(items)
              begin outer DB transaction before any SELECT
              resolve all parent metadata -> card -> currency/category
              insert/flush new statements (UUID conflict -> atomic 409)
              deterministic MerchantNormalizer (same session)
                alias lookup; insertion savepoints on misses
              construct ordinary Transaction rows in input order
              flush -> validate response snapshots -> commit once
                              |
                    router returns JSON 201
```

| Layer | Responsibility |
|---|---|
| `app/api/v1/transactions.py` | Add two POST handlers; inject `get_session`; pass `[payload]` or `payload.transactions`; translate domain failures; shape single/batch output. No business validation or commits in handlers. |
| `app/schemas/domain.py` | Add nested `StatementMetadataCreate`, extend `TransactionCreate`, bounded batch schemas; evolve `StatementResponse` with nullable files/source. Retain `TransactionResponse` and PDF input requirements. |
| `app/services/transaction_creation.py` (new) | `TransactionCreationService(session).create_many(items: list[TransactionCreate]) -> list[TransactionResponse]`; own request-wide parent resolution/creation, category policy, enrichment, transaction boundary and ordered snapshots. No FastAPI imports. |
| `app/services/merchants.py` | Preserve deterministic normalization, lookup, defaults, return tuple and PR1's existing insert-scoped savepoints. Never commit caller work. |
| `app/models/statement.py` + Alembic | Nullable file columns and non-null statement source; preserve parent FK, PDF defaults and card/hash uniqueness. New forward migration, never rewrite baseline. |
| `app/services/ingestion.py` | Explicitly assign `source=pdf` when constructing a PDF statement; keep file storage and all orchestration unchanged. |

The single endpoint delegates to exactly the same service path as batch; no duplicate creation algorithm or ingestion DTO adapter. The service takes a fresh request session with no active transaction or unrelated writes. Tests seed through a separate session. Do not silently join an arbitrary caller transaction or commit someone else's work. `get_session` already provides fresh sessions, `expire_on_commit=False`, exception rollback and no automatic success commit.

## Request and response schemas

### Single input

Retain `TransactionCreate.model_config = ConfigDict(extra="forbid")`; add `category_id: UUID | None = None` and `statement: StatementMetadataCreate | None = None`. Client-controlled transaction `id`, `merchant_id`, `low_confidence`, `recurring_rule_id`, top-level card identifiers and timestamps remain forbidden.

| Field | Contract |
|---|---|
| `statement_id` | Required non-null UUID in every item, including new-parent items. Missing parent with no request-wide metadata is content 422. |
| `statement` | Optional creation metadata; null means omitted. Existing parent plus any valid non-null object is 409 even if matching; it is never an update or equality assertion. |
| `date` | Required Pydantic date; document ISO `YYYY-MM-DD`. No ingestion date parser or fallback to today. |
| `description` | Required string, existing length 1–500. Preserve verbatim; do not strip or truncate financial data. Whitespace-only input remains schema-valid under existing constraints and may have no merchant. |
| `amount` | Required signed finite Decimal, at most 15 digits and 2 decimal places; storage range ±9,999,999,999,999.99. Zero and negative values are allowed. |
| `currency` | Required string of exactly 3 characters; service enforces exact supported value and parent match. No case folding, trimming, conversion or default. |
| `category` | Optional deprecated string, existing maximum 50; preserve existing allowance for empty strings. Schema validation still applies even if category ID takes precedence. |
| `category_id` | Optional UUID/null. Empty string is invalid JSON UUID (422); PATCH's form clear sentinel is not reused. Null and omission both select the legacy/no-category branch. |
| Installments | Retain independent `ge=1` number/total and nullable Decimal value constraints. Do not require pairs, enforce number ≤ total, calculate totals or infer a schedule. Values exceeding PostgreSQL signed INTEGER capacity are content errors (422), not accounting rules. |
| `raw_json` | Optional object/array/null, copied unchanged. Describe as caller metadata, not verified PDF/LLM provenance or a deduplication key. |

Money is Decimal end-to-end with no rounding to make invalid input fit. The schema module's prose claims float rejection, but current fields have no before-validator enforcing it. Add a creation-only before-validator for `amount` and `installment_value` rejecting Python float/bool inputs; accept decimal strings and integers, then apply Decimal constraints. Document JSON decimal strings (e.g. `"-1234.50"`); fractional JSON numbers are rejected with 422. Do not change response serialization, which continues using the existing Decimal JSON representation.

### Nested statement metadata and shared batch definition

`StatementMetadataCreate` is a closed object with exactly four required fields: `credit_card_id: UUID`, `period_start: date`, `period_end: date`, `statement_date: date`. Enforce `period_start <= period_end` (422). Do not require statement date or transaction date to fall inside the period. Reject nested `id`, files, source, status, errors, currency and timestamps; `statement_id` outside the object is the sole new-parent ID. Keep existing `StatementCreate` as PDF-specific input with required non-empty path/64-character hash; do not relax it or reuse it for API metadata.

For each distinct new UUID, **exactly one item carries the metadata object**, anywhere in the batch; all other references omit it or use null. This follows the revised proposal's one-object rule: additional objects return 422 at the second metadata-bearing item's index, whether identical or conflicting. Never merge fields, select the last definition, infer a parent by card/month, or require metadata on the first reference. All linked items use the exact supplied four-field definition without adjustments. Existing-parent metadata remains 409, not a matching/repeated-object exception. Pydantic schema errors are evaluated first; valid metadata never overrides persisted state.

### Statement persistence and response evolution

- `Statement.file_path: Mapped[str | None]` (`String(512)`) and `file_hash: Mapped[str | None]` (`String(64)`) become explicitly nullable. Preserve the hash index and `uq_statements_credit_card_id_file_hash`: ordinary PostgreSQL UNIQUE allows multiple null hashes on one card; equal non-null hashes on one card conflict, and equal hashes on different cards remain allowed. No `NULLS NOT DISTINCT`, sentinel, generated hash or uniqueness on period/source.
- Align the ORM statement primary-key constraint name explicitly with baseline `pk_statements` (no database PK replacement). `Base` has no naming convention, so plain `create_all` otherwise produces `statements_pkey` and cannot exercise exact-name conflict mapping faithfully. Verify PK-name parity on migrated and ORM-created test schemas.
- Define `StatementSource(enum.StrEnum)` with `PDF="pdf"`, `API="api"`; persist lowercase values using a non-native SQLAlchemy enum (length 3, `values_callable`, validation) and a named CHECK `ck_statements_source`. Add non-null `source` with ORM default PDF and database default `'pdf'`. Keep metadata and migration constraint definitions aligned. No transaction-level source field.
- Add `alembic/versions/0002_statement_source.py`, revision `0002_statement_source`, following `0001_postgresql_baseline` (reconfirm head before apply). Upgrade adds source with server default/backfill `pdf` and CHECK, and drops file NOT NULL constraints in one DDL transaction. Existing paths, hashes, statuses, errors, timestamps and child rows remain untouched; retain the default for old PDF writers. API writes explicitly override it with API source and `COMPLETED`, null files/error, supplied UUID/card/dates. Do not change the model's PDF `PENDING` default.
- `StatementResponse` exposes required nullable `file_path`/`file_hash` and `source: StatementSource`; retain other fields. Change transaction-list prose from “extracted” to “attached”. Both statement GET and PDF-upload response use this schema. `completed` for API means the atomic write finished, not PDF extraction or financial completeness. API additions to existing PDF/failed parents leave source/status/error and timestamps unchanged.
- Source describes parent creation, not every child transaction's origin. Existing/demo rows are backfilled as PDF under the compatibility rule; this is not verified historical evidence. Later PDFs remain separate statements; source never triggers reconciliation.

### Migration runner prerequisite and consumer audit

`alembic/env.py` currently rejects every populated database unless already at head, and its at-head early return also bypasses downgrade. Narrowly revise it to allow normal Alembic traversal for recognized versioned databases, let Alembic handle at-head no-op/downgrade, and apply the empty-database guard only to unversioned initialization. Unknown/multiple unsupported revision states must fail; never auto-stamp or baseline a populated unversioned database. Keep guard queries and migration DDL under a correctly committed transaction despite SQLAlchemy autobegin. Preserve baseline refusal/failure-rollback tests; replace `test_only_postgresql_baseline_revision_exists` with exact expected lineage tests.

Observed consumers: statement GET/upload serialize `StatementResponse`; ingestion hashes a real input `Path` and searches by non-null hash, not by API parent files; seed lookup uses its explicit non-null marker hash. No production file dereference beyond those paths was found in the `app/` file-field search. Audit schema exports, fixtures, web/seed readers and strict typing during apply; make only necessary null/source compatibility changes. Do not route API parents into PDF parsing or remove `_save_upload`. ORM `create_all` fixtures cannot prove the Alembic upgrade works; use migration-runner integration tests on populated baseline databases.

### Batch and success output

- `TransactionBatchCreate`: closed object containing only `transactions: list[TransactionCreate] = Field(min_length=1, max_length=200)`.
- Enforce the same bounds defensively at the service boundary; never truncate, chunk into independent commits or accept partial input. Duplicate-looking items remain distinct rows.
- `TransactionBatchResponse`: `transactions: list[TransactionResponse]`, `count: int`; count is constructed from the committed response list, not supplied by the client.
- Single returns 201 with `TransactionResponse`; batch returns 201 with `{"transactions": [...], "count": N}`. Response position equals input position, regardless of posting date or statement UUID. No 207, pagination, HTML negotiation or job polling.
- `TransactionResponse` does not expose `merchant_id` or `recurring_rule_id`; leave it that way. Verify merchant linkage through database assertions, not invented response fields.
- Populate UUIDs and timestamp defaults during flush. Build Pydantic response snapshots inside the transaction so missing fields fail before commit. PostgreSQL RETURNING supplies scalar defaults; any necessary refresh happens before commit, not afterward. Return snapshots only after the outer context exits successfully.

Example single body (UUIDs must identify existing rows):

```json
{
  "statement_id": "11111111-1111-4111-8111-111111111111",
  "date": "2026-09-10",
  "description": "LIDER COM 3",
  "amount": "12500.00",
  "currency": "CLP",
  "category_id": "22222222-2222-4222-8222-222222222222"
}
```

Example new-parent body (statement UUID must be unused; card UUID must exist):

```json
{
  "statement_id": "44444444-4444-4444-8444-444444444444",
  "statement": {
    "credit_card_id": "33333333-3333-4333-8333-333333333333",
    "period_start": "2026-09-01",
    "period_end": "2026-09-30",
    "statement_date": "2026-10-01"
  },
  "date": "2026-09-10",
  "description": "LIDER COM 3",
  "amount": "12500.00",
  "currency": "CLP"
}
```

Wrap bodies in `{"transactions": [...]}` for batch. A mixed batch may contain an existing-parent ID-only item, a new-parent ID-only item, then another item for that new UUID carrying its sole metadata object. All three are validated before any write. Subsequent requests for the new UUID omit `statement`; copying the creation body after success returns 409.

## Validation and enrichment data flow

1. Enter `async with session.begin()` before the first database read. Validate batch bounds and collect per-UUID reference indexes and every non-null metadata object. Fetch distinct persisted statement IDs/card IDs with scalar projections (avoid eager child collections). Classify existing/new parents, then build one request-local parent plan for each UUID; metadata can appear after its first referencing item.
2. Validate parent plans before writes: existing plus metadata -> 409 at the first metadata-bearing index; missing plus zero objects -> 422 at the first reference; missing plus multiple objects -> 422 at the second metadata-bearing index. For multiple invalid plans select the lowest offending index. This parent-definition pass precedes per-item validation. Fetch distinct cards from both persisted parents and new metadata, and the category set once each. Unknown new-parent card -> 404 at its metadata-bearing index; missing persisted-parent card -> internal 500. Then validate items in input order for currency and explicit category. Build `{category.name.lower(): category}` for merchant defaults; never load unbounded merchant/alias caches.
3. Supported currency is exactly `CLP` or `USD`, and it must equal `CreditCard.currency`. An unsupported three-character code (including lowercase), unsupported parent currency, or supported-but-mismatched code is 400. Malformed length/type is 422. A batch may span CLP and USD parents as long as each item matches its own parent. No `Statement.currency` exists.
4. Existing parent existence is sufficient: accept pending, processing, completed or failed statements, inactive cards and dates outside the billing period. Do not change existing metadata, source, status, errors or timestamps. For new parents, also allow inactive cards and out-of-period transactions. After all validation, insert each new `Statement` in canonical UUID order and flush individually before enrichment, using the explicit API assignments above. Insert ordering prevents reversed multi-parent requests from deadlocking on parent keys; it does not change response order. Creation does not coordinate concurrent ingestion.
5. Determine category state by the table below. Category lookup is by ID, never by the legacy string. Use `Category.name`, not `display_name`. Log one deprecation warning per service invocation if any item actually uses legacy category; do not log descriptions, amounts or raw metadata. ID-precedence items alone do not trigger it.
6. Resolve eligible descriptions using only `MerchantNormalizer.resolve_merchant(session, description, categories_by_name)`. Ignore its confidence/was-new flag for creation. Known merchant defaults may populate a new merchant's `default_category_id` but never infer a transaction category. The optional LLM method is never called, even when its deployment flag is enabled.
7. Construct ordinary `Transaction` objects with explicitly assigned category fields, confidence, merchant ID and allowed input fields. Use an explicit field allowlist; never pass the nested `statement` DTO through `model_dump()` into the ORM relationship. Leave recurring linkage null. Keep the ordered list and add/flush it after all enrichment completes; do not attach partially built transactions via parent relationships during enrichment.
8. Validate response snapshots, exit the transaction (one commit) and return. Any exception propagating out of the outer context rolls back every request-created statement, transaction, merchant and alias. No parent, alias or transaction is independently committed; no failed API parent survives for error tracking. The router never converts exceptions into a success body.

| Validated input | Stored `category_id` | Stored `category` | `low_confidence` |
|---|---|---|---|
| Non-null category ID, with/without legacy string | Supplied existing ID | Canonical name | `False`, even for new/unknown/no merchant |
| No ID, non-null legacy string (including empty) | Null | Exact supplied string | `True`, even if it matches a seeded category name |
| No ID and no non-null category | Null | Null, not `"Uncategorized"` | `True` |

### Merchant storage bounds

Repository evidence: transaction descriptions allow 500 characters, but `Merchant.name` allows 100 and `MerchantAlias.alias_text`/`normalized` allow 200. Blind reuse can turn valid transaction input into a database length error.

In the creation service, compute the existing deterministic `normalize(description)` solely for an eligibility check. If raw description exceeds 200 or the canonical value exceeds 100, leave `merchant_id=None` and do not call the resolver. Otherwise call the existing resolver, including its empty-canonical handling. This conservative guard also skips potential alias hits for oversized descriptions; it keeps acceptance independent of pre-existing alias data. Preserve the entire transaction description and category policy. Never truncate merchant identity, swallow arbitrary database errors as an unresolved merchant, or narrow the public description limit. This creation-only adaptation leaves ingestion's length behavior unchanged.

## Atomicity decision: fail parent races, recover merchant races

### Parent UUID conflict boundary

The persisted PK `pk_statements` is the authority for competing new UUIDs. During each isolated parent flush, catch only `IntegrityError` with SQLSTATE `23505` and that exact constraint name; asyncpg details may be on `exc.orig` or its `__cause__`. Raise domain `statement_creation_conflict` immediately and allow it to escape the outer `session.begin()` context, rolling back all earlier parent writes. The router maps it to 409 only after rollback. Attribute it to the attempted parent's metadata-bearing index, field `statement_id`. Do not classify card/hash, FK, deadlock or connection failures as this conflict.

No parent upsert, savepoint-and-reuse, SELECT-for-update on an absent row, retry loop or post-conflict winner lookup. READ COMMITTED plus PK enforcement makes a competing insert wait: after winner commit, loser gets 409; if the other transaction rolls back, a surviving insert can succeed. Otherwise valid competing requests for the same new UUID yield one 201 and one atomic 409 even when metadata matches. Client explicitly retries the whole failed batch with ID-only linkage for now-existing parents and keeps metadata only for still-missing parents, verifying the winning parent/card first. No server automatic retry. The stable UUID does not make child creation idempotent.

### Historical merchant hazard and current prerequisite

The former deterministic conflict handlers called session-wide rollback, discarding caller writes. Current `app/services/merchants.py` already uses insert-scoped savepoints; [apply-progress.md](apply-progress.md) records PR1 and 250 authored lines of implementation/tests (not re-verified here). Preserve this prerequisite and test new parents across it; do not duplicate or silently rewrite PR1. A subsequent commit cannot restore rows discarded by `Session.rollback()`, and an outer savepoint cannot contain such a call.

### Chosen correction

Retain the two insert boundaries **inside `resolve_merchant`**, without changing its signature or callers:

```text
flush any pre-existing pending work before the guarded insert
try:
    async with session.begin_nested():
        add candidate merchant (or alias) INSIDE this block
        flush candidate
except IntegrityError:
    nested context has rolled back only this insert
    if not the relevant uniqueness conflict: re-raise
    re-query the winning merchant by name (or alias by exact alias_text)
    if no consistent winner exists: re-raise
    continue with winner; no Session.rollback(), no commit
```

- Separate merchant and alias savepoints allow a merchant-name conflict to bind a new alias to the existing merchant, preserving intended recovery behavior (including existing merchants with no matching alias).
- SQLAlchemy `begin_nested()` unconditionally flushes pending state *before* establishing its savepoint. Add candidates only after entering. Explicitly flush preceding caller state outside the recovery `try`, so unrelated caller flush failures cannot be mistaken for merchant uniqueness races. Ingestion may already have a pending statement; it must remain in the outer transaction.
- Recover only PostgreSQL unique violations for the intended merchant-name or raw-alias key. Do not swallow FK, length, connection or other integrity failures. Recover by the exact unique key; an absent winner propagates failure. Current alias recovery treats raw alias ownership as authoritative, without a new normalized-equality gate. Do not re-add rolled-back candidate objects or use their IDs.
- Alias uniqueness is on **raw `alias_text`**, not `normalized`. Use the exact unique raw key for alias-conflict recovery; keep ordinary normalized lookup unchanged. The normalized index is non-unique: multiple matching rows can still cause an ambiguous lookup. Fail the whole creation request (500) rather than arbitrarily choosing a merchant. Repairing that identity model or adding a uniqueness migration is deferred.
- Preserve existing return flags and normal-path merchant/default/source semantics. No ingestion category policy, cached alias logic, PDF parsing, statement lifecycle or recurring orchestration changes. The deterministic shared correction is intentional; retaining discarded caller writes is the bug fix, not a new ingestion workflow.
- `resolve_merchant_with_llm` is untouched and retains its existing rollback hazard. Creation cannot reach it. Do not claim this change repairs every ingestion transaction boundary.
- Savepoint release is not a durable commit: a later normalization, transaction flush, response validation or commit failure still rolls back all request-owned rows. Existing rows and independently committed concurrent winners are not request-owned and must remain.

READ COMMITTED is sufficient for atomic writes and querying a committed uniqueness winner; no automatic request retry or stronger isolation is introduced. Parent/category validation observes the database at lookup time. Concurrent deletion or update can race validation; database FK failures abort the request, not become fabricated 404s. This slice does not promise serialization with card edits, category renames or ingestion. A lost connection during COMMIT or response delivery can leave the outcome unknown to the client, but never makes a partial batch durable.

## Error mapping

Define a small service exception carrying `code`, safe `message`, optional `field` and optional zero-based `index`. Keep HTTP status selection in the router. Do not introduce a global exception handler or change GET/PATCH errors.

| Condition | HTTP / mapping |
|---|---|
| Invalid JSON, unknown fields, malformed UUID/date/Decimal, invalid lengths/installments, batch bounds | 422. Preserve FastAPI's standard validation detail list; item locations include `["body", "transactions", index, field]`. Service-only storage/bounds checks use the domain envelope below. |
| Missing required `statement_id` / malformed or null UUID | 422 schema error, even when metadata is supplied. |
| Missing parent with no metadata anywhere for its UUID | 422, `statement_metadata_required`, field `statement`, first referencing index. |
| Additional metadata object for one new UUID (identical or conflicting) | 422, `duplicate_statement_metadata`, field `statement`, second metadata-bearing index. |
| Existing parent plus non-null metadata, matching or not | 409, `statement_already_exists`, field `statement`, first metadata-bearing index. |
| Lost new-parent PK race | 409, `statement_creation_conflict`, field `statement_id`, metadata-bearing index; retry message explicitly requires ID-only linkage. |
| Unknown card in new metadata | 404, `credit_card_not_found`, field `statement.credit_card_id`, metadata-bearing index. |
| Missing explicit category UUID | 404, `category_not_found`, field `category_id`; never fall back to the legacy string. |
| Unsupported item/parent currency or mismatch | 400, `unsupported_currency` or `currency_mismatch`, field `currency`. |
| Recovered merchant/alias uniqueness conflict | No error if all rows subsequently commit; full ordered 201 only. |
| Unrecoverable enrichment, integrity failure, invalid parent linkage, ambiguous alias, flush/commit or unexpected failure | 500, generic `creation_failed`. Log diagnostic context server-side without request contents or SQL parameters. Never expose database exception text or report partial success. |

For new routes, domain errors use `{"detail": {"code": "...", "message": "...", "field": "...", "index": 1}}`. Omit field/index when inapplicable; single-route mapping omits index. Batch errors during a specific item's validation/enrichment include its index. Final flush/commit failures may affect the whole request and omit index rather than attributing them to the last item. Schema list errors such as 0/201 items locate `transactions`, not a nonexistent item; nested schema errors locate `statement` plus the nested field. Parent-definition indexes follow the prepass above. Merchant races retain internal recovery; parent creation intent conflicts require explicit client action, never automatic retries.

## Alternatives and tradeoffs

| Alternative | Decision and rationale |
|---|---|
| Statement-less or synthetic placeholder creation | Rejected: keep mandatory FK and caller UUID/card/dates. Explicit file-free API parents replace the old existing-only prerequisite without fabricating PDF evidence or inferring periods. |
| Parent upsert / accept matching existing metadata | Rejected by confirmed contract: metadata is creation intent. Return 409 even if matching; never silently bind race losers to a winner. |
| Repeat/merge metadata per item | Rejected in favor of the proposal's single authoritative object per new UUID; duplicates/conflicts return 422, independent of item order. |
| API parent pending status / stop PDF saving | Rejected/deferred: API transaction sets commit with completed parents and no job; PDF lifecycle/storage remain separate and intact. |
| Idempotency key or heuristic deduplication | Deferred: durable request fingerprint, replay response, key scope and concurrent semantics need their own design/storage. Date/amount/description similarity cannot distinguish legitimate equal charges. Every successful submission appends, including duplicate items in one batch. |
| Shared ingestion transaction factory | Deferred: LLM amount/date parsing, string taxonomy resolution, confidence OR rules, placeholder categories and ingestion lifecycle differ from explicit JSON input. Sharing the deterministic normalizer is enough; extraction would broaden review and risk GET/PATCH/ingestion drift. |
| Per-item commits / independently committed merchants | Rejected: neither satisfies request-wide rollback; enrichment must not survive a failed creation request. |
| Outer savepoint around the former rollback-based normalizer | Rejected: session-wide rollback destroys the outer transaction; PR1 fixes the insert boundaries instead. |
| Creation-only fail-on-conflict switch | Viable minimal safety fallback, not selected: leaves shared deterministic rollback unsafe for ingestion and fails recoverable existing-merchant/race cases. Internal savepoints retain intended winner recovery without a new behavior flag. |
| PostgreSQL upserts or shared session wrapper | Deferred/rejected here: upserts could avoid exception recovery but require reworking ORM identity/default handling and alias winner selection; a wrapper obscures transaction ownership. Two local savepoint boundaries are easier to review. |
| Merchant-based category inference / LLM | Rejected: explicit ID must stay authoritative and absent category means absent category. No hidden cost or inference on this recovery API. |
| Bulk insert or parallel item resolution | Rejected for 200-item slice: ORM flush supplies IDs/defaults and existing enrichment semantics; concurrent tasks cannot safely share one AsyncSession. Bounded sequential work is simpler, though high-uniqueness batches cost more round trips. |

## File plan and verification

| File | Planned change / evidence |
|---|---|
| `app/models/statement.py`, `app/models/__init__.py` | Nullable files, source enum/default/CHECK/exports, PK-name parity with baseline; update PDF-only prose, preserve status defaults and uniqueness. |
| `alembic/versions/0002_statement_source.py` (new), `alembic/env.py` | Additive migration/backfill, safe guarded downgrade, narrowly permit versioned migration traversal while protecting unversioned initialization. Baseline immutable. |
| `app/schemas/domain.py`, `app/schemas/__init__.py` | Nested metadata, category ID, money validation, batch contracts/exports; nullable statement response/source, PDF-specific input retained; transaction response unchanged. |
| `app/services/transaction_creation.py` | New service, parent planning/creation and PK conflict mapping, local errors, validations, enrichment and atomic ordered persistence/snapshots. |
| `app/api/v1/transactions.py` | Two thin POST routes and local error mapping/OpenAPI descriptions; existing handlers unchanged. |
| `app/services/merchants.py` | Preserve already implemented PR1 deterministic savepoints; no new change expected. |
| `app/services/ingestion.py` | Explicit PDF source assignment only; preserve file/LLM/recurring/lifecycle pipeline. |
| `app/api/v1/statements.py`, `app/cli/seed_demo.py`, `app/web/` | Audit reader/file assumptions; no behavioral rewrite expected; narrow compatibility fix only if evidence requires it. |
| `tests/test_transaction_creation.py` (new) | Parametrized schema, service and PostgreSQL-backed HTTP creation tests using existing fixture patterns. |
| `tests/test_merchants.py` | Preserve PR1 regressions; creation-flow tests prove API parents survive recovery and roll back on later failure. |
| `tests/test_alembic.py`, `tests/test_models.py` | Real upgrade/downgrade and runner guards, ORM/migration parity, nullable hash uniqueness, provenance/status/default tests. |
| `tests/test_ingestion.py`, `tests/test_web_phase1.py`, statement schema/HTTP tests | PDF saving/source/dedup/lifecycle regression and API-parent statement GET/null serialization; update exact-response expectations. |
| `README.md` | Existing/new/mixed examples, sole batch metadata definition, 409 ID-only retry, response evolution, provenance/completed meaning, unchanged PDF storage, category/currency/200-item policy and duplicate/timeout warning. |
| Current change `specs/transactions-api/spec.md`, `tasks.md`, apply/verify reports | Parent must align/forecast in subsequent phases; not edited here. No legacy changes. |

Strict TDD per behavior unit: record RED failure, GREEN focused result, TRIANGULATE boundaries/conflicts, then REFACTOR and regressions. No tests were run in this design-only phase.

Required coverage:

1. **Schema:** minimal input and optional metadata/installments; required statement UUID even with metadata, four required nested fields, null-as-omitted, forbidden nested source/status/files/ID, reversed/equal periods, statement date outside period; malformed/missing/extra fields at all nesting levels; null/empty category ID; signed/zero/max money; excess precision/range/non-finite/float/bool money; positive installment bounds and INTEGER overflow; 0, 1, 200, 201 items. Assert indexed 422 locations and no writes.
2. **HTTP happy paths:** single 201 visible through existing GET; batch ordered despite reversed dates, multiple statements/cards/currencies and repeated descriptions; count and persisted UUID sets exactly match. Existing response keys and Decimal encoding are unchanged. Duplicate-looking input creates distinct IDs, including repeated identical requests.
3. **Domain policy:** missing metadata 422, unknown new-parent card/category 404 and currency cases with later invalid indices; existing-parent matching/mismatching metadata 409 with no writes; sole new-parent metadata on first/middle/last item, duplicate identical/conflicting objects 422 at second index, null/omitted followers, two new parents/mixed currencies, ordered outputs and one parent per UUID. Assert supplied UUID/dates/card, null files/error, API source/completed and readable statement GET. Valid inactive cards and non-completed existing statements/out-of-period dates remain accepted; existing parent metadata/source/status/timestamps unchanged. Parametrize category precedence, null/omitted/empty legacy values, seeded-looking legacy string, known/unknown/empty merchant and at-most-one warning.
4. **Enrichment:** alias hit; known and unknown merchant creation; empty canonical; eligibility edges at raw 200/201 and canonical 100/101 with full description preserved. Spy that PDF, LLM and recurring services are never invoked, including LLM flag enabled.
5. **Outer atomicity:** inject a failure after earlier merchant/alias writes; force final transaction flush and pre-commit response-validation failures; simulate a commit failure before durability. In a fresh independent session verify no request-created statements, transactions, merchants or aliases remain and pre-existing rows/parent state are unchanged. Include multiple flushed new parents before the failure, as well as recovered merchant conflicts followed by late failure. Test service rollback directly, not only dependency cleanup.
6. **Real conflict recovery:** force actual PostgreSQL merchant-name and raw-alias uniqueness violations, not just a lookup hit. Commit a conflicting winner in a second session after the request lookup and before its insert using barriers/controlled interception. Assert the losing insert's savepoint rolls back, preceding outer writes survive, all response IDs exist after success, and a subsequent injected failure removes every request-owned write while leaving the winner intact. Also test an existing merchant with no alias, and an unrecoverable/non-unique integrity failure.
7. **Regression:** existing transaction GET filters, PATCH form/HTML/JSON-response and clear-sentinel tests, merchant API/normalization tests, category tests and ingestion tests. Assert PDF upload still writes identical bytes under upload directory, hashes real content, returns PDF source/non-null files, deduplicates by card/hash and preserves lifecycle/failure/recurring behavior. API calls must not create any files; later PDF with similar period/transactions remains separate. Include a pending ingestion-style statement before a merchant conflict to prove savepoint recovery preserves caller work. The existing `test_resolve_merchant_integrity_error_race_guard` explicitly exercises only a lookup hit and is not conflict evidence.

8. **Parent race:** real PostgreSQL two-session/barrier tests with matching and different valid metadata: both observe absence, one commits, loser receives 409 with no request-owned writes. Include earlier inserted parent(s) in the loser, reversed multi-parent input order, and winner rollback allowing the other insert to succeed. Assert no automatic second attempt, no winner modification, explicit ID-only retry succeeds, resending metadata still returns 409. Use independent durable-state assertions; a sequential existing-parent conflict is not race evidence.
9. **Migration:** empty-to-head and populated-baseline-to-head via Alembic runner preserve financial/file/status/timestamp data and stamp, backfill all old source values as PDF, and keep server default for old writers. Test PDF same-card hash rejection, cross-card allowance, multiple same-card null hashes, invalid/null source rejection and ORM defaults. Test safe head-to-baseline downgrade and upgrade again; unsafe downgrade with API rows (even non-null files) or either null file field must fail before any DDL/stamp change and preserve all data. Retain unversioned populated refusal, baseline failure rollback, head no-op, unknown-revision failure and offline SQL guard coverage. `create_all` alone is not migration evidence.

Use disposable PostgreSQL fixtures from `tests/conftest.py` and the empty `postgres_database` fixture in `tests/test_alembic.py`; never SQLite or a production database. Planned focused invocation:

```sh
POSTGRES_USER=finhealth POSTGRES_PASSWORD=secret POSTGRES_DB=finhealth \
POSTGRES_TEST_HOST=127.0.0.1 POSTGRES_TEST_PORT=5432 \
POSTGRES_TEST_USER=finhealth POSTGRES_TEST_PASSWORD=secret \
pytest tests/test_transaction_creation.py tests/test_merchants.py tests/test_transactions.py \
  tests/test_alembic.py tests/test_models.py tests/test_ingestion.py tests/test_web_phase1.py
```

Then run the complete pytest suite with the same explicit test database settings, `ruff check app tests`, `ruff format --check app tests`, `mypy --strict app/`, and `./scripts/verify.sh` for the documentation change. Record commands, results and skip reasons. Skipped database tests are not atomicity proof. Real PDF E2E remains environment-dependent (`TEST_RUT` and local sample PDFs); report unavailable prerequisites rather than claiming it ran. Use HTTP ASGITransport with `raise_app_exceptions=False` when asserting generic 500 bodies, and independent sessions for durable-state assertions.

## Delivery, rollout and rollback

### Review workload and candidate chain boundaries

Historical apply progress records consent for a `feature-branch-chain` and PR1 implemented at 250 authored lines; this does not authorize an arbitrary expanded chain. Current session policy remains `ask-on-risk`; the parent must reconcile the recorded delivery decision and approve revised boundaries before further apply. The old task forecast was 1,000–1,500 lines; expanded scope is high risk, provisionally ~1,400–2,200 authored additions plus deletions across implementation/tests/docs/artifacts (planning estimate, not a measured diff). Count OpenSpec edits and existing uncommitted PR1 work in the correct base-relative review slices; do not count document length as changed-line size.

| Candidate behavior boundary | Dependencies / evidence and rollback |
|---|---|
| PR1: deterministic merchant savepoint safety (existing, 250 recorded lines) | Preserve code/tests and its recorded PostgreSQL evidence; independent rollback of only this normalizer fix, not a new implementation task. |
| Migration runner supports safe forward evolution (~100–200 lines) | Runner guards + upgrade/downgrade/no-op regression tests together, baseline unchanged. Roll back only before relying on later revisions; does not enable API writes. |
| File-free statement compatibility (~250–400 lines) | Migration/model/source/nullable readers + explicit PDF source + migration/PDF/GET tests and response/upgrade docs together. No creation routes yet. Rollback guarded by row compatibility below; retain readers once API rows exist. |
| Atomic creation service work units (~500–800 lines total; needs finer forecast) | Schema/service + focused policy, parent-race and full statement/merchant/alias/transaction rollback tests together before exposure. Split further into tested internal parent-planning and atomic persistence units only if each has a coherent finished state; never split by file type or defer essential failure tests. |
| Public single/batch exposure (~250–400 lines) | Depends on all safety units; both routes, indexed HTTP errors, 409 retry, mixed-parent ordered-response tests and user documentation together. Rollback removes POST wiring, retains compatibility and data. |

These ranges are provisional; shared integration coverage/OpenSpec work can increase any slice. Tasks must produce exact base-relative forecasts and keep every proposed PR <=400 authored additions+deletions or seek explicit `size:exception` consent. No revised chain strategy, exception or publication is inferred here. Do not keep the old “routes now, atomicity later” boundary. Each unit records RED/GREEN/TRIANGULATE/REFACTOR, focused commands/results, runtime scenario/results (or justified N/A), and independent rollback boundary. This design-only edit has no runtime boundary or executed test result; changed-line measurement remains for the parent because no command tool is available here.

### Rollout

1. Align current specs/tasks and resolve the expanded delivery-budget gate; run real PostgreSQL migration/atomicity/PDF regression and quality gates. Back up database and retain PDFs under existing operational authorization; do not mutate production as a test.
2. Deploy/use the updated migration runner and run the additive upgrade on the existing versioned database before source-aware app code. Test its lock duration on a representative copy and schedule an appropriate window. Never re-run the destructive baseline or stamp around a failed guard. Verify source backfill, file values and card/hash constraint.
3. Deploy compatible model/statement readers and PDF source assignment everywhere before enabling the two POST routes. Old writers can rely on database PDF default during the compatibility window, but old string-only readers MUST be drained before any file-free parent exists. No new feature flag is required; separate deployment of POST wiring is the exposure gate.
4. On disposable/designated test data, smoke existing-parent append, new-parent creation and mixed batch; verify returned transaction IDs and statement GET (`api`, null files, completed). Exercise existing-parent metadata 409, explicit ID-only retry, invalid mixed batch with unchanged counts across all four tables, and PDF upload persistence/dedup/read behavior. No ambiguous real financial entries as probes.

Preserve the self-hosted access boundary and deployment request-size limits: 200 items bounds rows, not arbitrary `raw_json` bytes or execution time. Document append-only/non-idempotent semantics: keep returned IDs; a timeout after COMMIT is ambiguous, so verify before resubmitting. A known 409 loser persisted none of its writes, but an ID-only retry can append duplicates of another request's transactions. Parent source/completed status do not certify financial completeness, provide transaction provenance or reconcile later PDFs.

### Rollback and guarded downgrade

Disable/remove POST wiring and creation-only service/input/batch schemas with their tests/docs as the exposure rollback. Retain source-aware ORM/statement responses, nullable file schema, PDF source assignment and every accepted row. Do not blindly deploy the pre-change application once API parents exist. The independently verified merchant savepoint fix may remain; revert only at its separate regression-tested boundary.

A separately authorized schema downgrade targets **only `0001_postgresql_baseline`**, never `base` (which drops financial tables). In the new migration's downgrade, acquire a transaction-scoped table lock preventing statement writes, then execute a PostgreSQL guard that raises if any row has `source <> 'pdf'`, `file_path IS NULL` or `file_hash IS NULL`. Run the guard before changing constraints/source, in the same DDL transaction; emit equivalent lock/guard SQL for offline downgrade rather than relying on Python-only inspection. Refusal leaves schema, revision and data unchanged. Only if compatible may it restore both NOT NULL constraints and drop source/CHECK. A safe no-API-row downgrade preserves PDF paths/hashes/statuses and non-null uniqueness; retain backups and independently verify before rolling readers back.

Never delete API parents/children, fabricate files or hashes, reclassify API source as PDF, or silently discard provenance to pass the guard. Parent relationships cascade deletion. Accepted-data corrections require explicit row-ID review/authorization outside this change. Removal of PDF storage remains a separate future design.

Persistence: Engram tools are unavailable; decisions and discoveries are saved in this OpenSpec artifact only. `skill_resolution: paths-injected` (both requested `SKILL.md` files loaded). No executor-specific phase skill was supplied; design execution follows the injected role and project OpenSpec rules without registry discovery or child delegation. Design revision is complete; further apply remains gated on current spec/task alignment and revised review-budget approval.
