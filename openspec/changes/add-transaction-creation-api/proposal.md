# Proposal: Transaction creation API with atomic statement creation

## Intent and decision

Add public JSON endpoints for single and batch transaction creation as a backup or alternative to PDF ingestion. Every transaction item MUST supply `statement_id`. An existing statement MUST be referenced by `statement_id` only, without nested statement metadata; supplying metadata returns HTTP 409 even when it matches. A missing statement can be created under that UUID using a nested `statement` object in the same atomic request and starts with `status=completed` because it is created with its transaction set.

This confirmed scope supersedes the exploration's existing-parent-only recommendation and the earlier no-migration proposal. It MUST include a migration, nullable statement file metadata, explicit statement source, and corresponding model/schema/response updates. PDF upload and file-saving behavior remain unchanged; removing PDF storage is a separate future change.

## Problem statement

Users and API clients need a supported way to add transactions when PDF extraction misses rows or no usable PDF exists. Requiring a previously ingested statement prevents the latter workflow. Explicit, caller-supplied statement metadata enables file-free creation without orphan transactions, fake file hashes, parsing, or an LLM.

## In Scope

### Statement linkage and metadata

| Decision | Required behavior |
|---|---|
| Mandatory linkage | Every transaction item supplies a non-null UUID `statement_id`; newly created statements use that supplied UUID. No nullable linkage or implicit card creation. |
| Existing statement | Clients MUST reference the parent by `statement_id` only and omit nested statement metadata. Any non-null `statement` object for an existing parent is a conflict (HTTP 409), even if every field matches. Creation metadata is only for a new statement; subsequent requests identify the existing parent by ID. Existing metadata, source, status and errors are never overwritten. |
| New statement | A missing parent requires a nested `statement` object containing `credit_card_id`, `period_start`, `period_end`, and `statement_date`. The card must exist. File metadata and source are not caller-controlled inputs; API creation stores null file fields, server-assigned `source=api` and initial `status=completed`, because the parent is created atomically with its transaction set. No ingestion or recurring job is scheduled. |
| Shared batch metadata | At most one non-null `statement` object per distinct `statement_id` in a request, even if duplicate objects are identical. Reject additional objects with 422 at the duplicate item's index. For a missing parent, exactly one item supplies metadata; all other items referencing that UUID omit it and share the resulting statement. The metadata-bearing item may appear anywhere in input order. Resolve request-wide parent metadata before writing dependent transactions. |
| Concurrent parent creation | If two requests attempt to create the same new statement UUID concurrently, one succeeds and the other fails atomically with HTTP 409. The losing client must retry using only the existing `statement_id`, omitting nested statement metadata for that parent. No automatic retry or silent reuse of the concurrently created parent. |
| Mixed batches | A batch may reference multiple existing and new statements; validate each transaction against its own resolved parent's card. No grouping by inferred card/month. |
| Metadata validation | Require valid UUID/date values and reject unknown nested fields. Require `period_start <= period_end`; do not invent a statement-date-within-period or transaction-date-within-period restriction. Missing required metadata for a nonexistent parent returns 422, not an unconditional missing-statement 404. |
| Persistence | Make `Statement.file_path` and `Statement.file_hash` nullable. Add non-null source with supported values `pdf` and `api`, a safe database/application default of `pdf`, and backfill existing statements as `pdf`. Preserve existing file paths and hashes without rewriting files. |
| PDF uniqueness | Preserve `uq_statements_credit_card_id_file_hash`: the same non-null hash on the same card remains unique; the same hash on another card remains allowed. PostgreSQL's ordinary nullable unique semantics allow multiple API statements with null hashes. Do not introduce nulls-not-distinct uniqueness or placeholder hashes. |
| Responses | Update `StatementResponse` to expose source and nullable file fields; update model types and relevant creation schemas consistently. Keep PDF input requirements intact through source-appropriate schemas. `TransactionResponse` remains unchanged. |

Repeated batch items for one new statement share exactly one metadata object, carried by one item rather than copied into every item. The one-object rule gives each new parent one authoritative definition, avoids conflicting copies and keeps batch order irrelevant. It does not permit metadata for an existing parent: an existing `statement_id` plus metadata remains invalid even when matching. A null nested value is treated as omitted. Statement source describes how the parent was created, not the origin of every transaction attached to it: API transactions added to a PDF statement do not change its source.

### Transaction API behavior retained

| Decision | Required behavior |
|---|---|
| Single creation | `POST /api/v1/transactions` accepts a JSON transaction and returns HTTP 201 with the existing `TransactionResponse`. |
| Batch creation | `POST /api/v1/transactions/batch` accepts `{transactions: [...]}` with 1–200 items and returns HTTP 201 with `{transactions, count}`. Preserve input order; count equals the number committed. |
| Atomicity | Commit all request-created statements, transactions, merchants and aliases together, or persist none of the request's writes. No independent parent/enrichment commit or partial-success response. |
| Input | Extend `TransactionCreate` with optional nested `statement` and optional `category_id`; retain date, description, signed Decimal amount, currency, installment fields and optional `raw_json`. Reject unknown fields and invalid values. Money remains `Numeric(15,2)`, never floating-point arithmetic. |
| Category ID | A supplied non-null `category_id` takes precedence over legacy `category`, stores the canonical category name, and sets `low_confidence=False`. Unknown IDs return 404. |
| Legacy category | Without a category ID, preserve a supplied free-form category, keep its FK null and set `low_confidence=True`. Document deprecation and log at most once per request following PATCH intent; do not silently resolve the string to an ID. |
| No category | Keep category and category ID null and mark low confidence. Merchant defaults do not override explicit or absent category input. |
| Merchants | Reuse deterministic `MerchantNormalizer.resolve_merchant`, including alias lookup and merchant/alias creation, without the optional LLM path. An unresolved merchant may remain null. Category confidence follows the rules above, not ingestion-specific merchant confidence. |
| Currency | Require exact supported CLP/USD matching the resolved statement's `credit_card.currency`, including for new statements. No conversion, inferred currency or new statement currency column. |
| Errors | Use 422 for schema/content validation, missing new-parent metadata and additional metadata objects for one new parent; 409 for metadata supplied for an existing parent (matching or not) and concurrent parent-creation conflicts; 404 for unknown card/category references; 400 for currency business-rule failures. Item-specific batch errors identify the zero-based input index. Database failures never return partial 201 results. |
| Compatibility | Preserve GET/PATCH behavior except the intentional statement-response evolution, and preserve the current PDF upload, file-saving, hashing, deduplication and ingestion lifecycle. Creation uses JSON, not PATCH's form/HTML transport or empty-string category-clear sentinel. |

Document examples for existing/new parents and mixed batches, limits, response evolution, provenance meaning, and duplicate/retry caveats. Add focused strict-TDD and PostgreSQL-backed coverage during implementation.

## Out of Scope

- Statement-less transactions, nullable transaction linkage, synthetic placeholder statements, automatic card creation, or a standalone statement-creation endpoint.
- Removing PDF storage, changing upload file-saving behavior, deleting existing PDFs, or refactoring PDF ingestion.
- Reconciliation, merging, replacement or deduplication against later PDF statements.
- Idempotency keys, transaction uniqueness constraints, automatic retry deduplication or partial batch acceptance.
- Recurring detection on creation, new recurring wiring, or promises of later backfill.
- LLM merchant resolution, automatic category inference, transaction-level provenance migration, or speculative reconciliation fields.
- PATCH changes, transaction deletion/edit expansion, UI entry forms, dashboard/query redesign, new authorization models or persistence backends.
- Broad shared transaction-factory extraction or edits to legacy OpenSpec changes.

## Affected Areas

| Area | Expected impact |
|---|---|
| `app/api/v1/transactions.py` | Two creation routes; preserve existing router/transport contracts. |
| `app/schemas/domain.py` | Nested metadata, bounded batch contracts, nullable statement response file fields and source; unchanged transaction response shape. Preserve PDF-specific input validation. |
| `app/models/statement.py` | Nullable file field types, source definition/default and preserved card/hash uniqueness. |
| `alembic/versions/` | New additive migration for file nullability and source/backfill, with guarded downgrade policy. Do not rewrite historical migrations. |
| Transaction creation service under `app/services/` | Request-wide metadata resolution, parent creation, category/currency checks, deterministic enrichment and atomic persistence within router → schema → service → model layering. |
| `app/services/merchants.py` | Reuse normalization; design must address session-wide rollback during uniqueness conflict recovery without changing ingestion semantics. |
| Existing statement consumers | Audit response serialization and file assumptions; only narrowly necessary null/source compatibility updates, not PDF storage changes or UI redesign. |
| `tests/` | Schema/service, migration, PostgreSQL HTTP/atomicity and unchanged-PDF-flow regression coverage. |
| API documentation / README | Existing/new statement examples, shared batch metadata, nullable response fields, source, retry limitations and no reconciliation. |

Existing queries will see additional ordinary rows and changed totals. Duplicate submissions can inflate spending. Statement source is not a transaction-origin marker or proof that a dataset is complete.

## Confirmed decisions and remaining assumptions

- Execution is auto and the user explicitly confirmed revised scope; no new interactive question round is required for those decisions. The initial `completed` status, existing-parent ID-only contract, shared new-parent batch metadata and concurrent-creation 409 behavior are confirmed, not open product decisions.
- Existing self-hosted single-user access boundaries remain unchanged; “public API” does not introduce a new internet exposure guarantee.
- Existing parents have no new completed-only, active-card or posting-date-within-period gate. Adding transactions does not mark failed statements completed or repair their error metadata.
- Retain existing installment validation rather than inventing accounting rules. Caller `raw_json` remains unverified metadata, not PDF provenance or a reconciliation key.
- Duplicate-looking transactions remain legitimate. A timeout after commit has an ambiguous outcome; clients must verify before resubmitting. A stable parent UUID does not make transaction creation idempotent.
- **Confirmed lifecycle:** API-created statements start as `completed` because their transaction set is created in the same atomic write. This means the API write completed, not that PDF extraction ran or all financial data is present. Do not apply the PDF `pending` default or schedule ingestion/recurring jobs.
- **Confirmed parent conflict contract:** metadata expresses creation intent, never an update or matching assertion for an existing statement. Existing-parent metadata returns 409 even when identical. A concurrent creator losing the UUID race receives 409 with all its writes rolled back and must retry with only the existing `statement_id` for that parent, without automatic retry. Exact database mechanism belongs in design.

## Risks and mitigations

| Risk | Mitigation / required follow-up |
|---|---|
| Merchant race recovery discards earlier batch writes | `resolve_merchant` uses `session.rollback()` on uniqueness conflicts. A loop plus final commit is insufficient. Design safe conflict boundaries or fail the entire request; test that parents and earlier rows cannot silently disappear. |
| Nullable files break readers or old deployments | Audit statement serializers/consumers, expose explicit nulls/source, test API-parent reads, and deploy the additive migration before compatible application code. Do not claim pre-change readers can consume file-free statements. |
| Migration changes PDF deduplication | Preserve the existing card/hash constraint and non-null hash behavior. Test same-card rejection, cross-card allowance, and multiple null-hash API parents. |
| Provenance overclaims transaction origin | Define source as statement creation provenance; mixed-origin child transactions remain possible. Preserve PDF source when appending via API. |
| Retry or later PDF ingestion double-counts spending | Document append-only, non-idempotent and unreconciled behavior. Later PDF upload remains a separate statement, not a match/update to the API parent. |
| Statement lifecycle misleads consumers | Set new API parents to `completed` with their atomic transaction set; document that this does not certify financial completeness or PDF extraction. Preserve existing-parent status/errors and PDF lifecycle; schedule no ingestion jobs. |
| Clients resend creation metadata or race on a parent UUID | Reject existing-parent metadata with 409 even when matching; never silently reuse a concurrent winner. Roll back the losing request fully and document an explicit client retry with ID-only linkage. Test both already-existing and concurrent-parent conflicts. |
| Category/merchant conventions drift | Reuse deterministic normalization and PATCH precedence; test category confidence independently. |
| Batch limit is mistaken for a byte limit | Enforce 1–200 items and field limits; retain deployment request-size protections. Arbitrary `raw_json` is not bounded solely by row count. |
| Expanded persistence scope exceeds review budget | Re-forecast the existing feature-branch chain with behavior/tests/docs together, including migration compatibility. Respect the 400 authored-line threshold; seek a delivery decision under ask-on-risk rather than assuming `size:exception`. Do not alter or commit already implemented PR1 work in this phase. |

## Rollback plan

Disable/remove the two creation routes and creation-only wiring, tests and documentation as a behavior boundary while retaining schema and reader compatibility for accepted API-created statements. Preserve GET/PATCH, PDF ingestion, files and existing shared merchant behavior.

Do not blindly deploy the old application or restore file `NOT NULL` constraints after file-free rows exist: old response schemas require strings. Keep nullable fields and source support until a separately authorized data-compatibility plan permits downgrade. A downgrade MUST refuse incompatible null-file/API rows rather than delete them, fabricate file paths/hashes or silently discard provenance. Verify downgrade preconditions before modifying the schema.

Previously accepted financial data, statements and merchants/aliases remain intact. Never remove parent statements as rollback cleanup: transaction relationships cascade deletion. Any data correction requires explicit row-ID review and authorization outside this change.

## Success criteria

- Valid single requests against existing or new parents return 201 and readable transaction responses without PDF/LLM work; every item retains mandatory `statement_id`.
- New parents require all four metadata fields and an existing card, persist under the supplied UUID with `source=api`, `status=completed` and null files, and are readable through updated statement responses.
- Existing-parent requests use `statement_id` only, without nested metadata. Supplying metadata returns 409 without writes, whether matching or mismatching; existing metadata, source, status and errors remain unchanged.
- Two otherwise valid requests concurrently creating the same new statement yield one success and one atomic 409. The loser is not automatically retried; an explicit retry omits metadata for that now-existing parent and uses only its `statement_id`.
- Batches of 1 and 200 succeed with ordered responses and accurate counts; 0 and 201 fail. Mixed parents and metadata appearing after its first referencing item work. Repeated items for one new statement share one metadata object; additional objects fail with 422 even when identical. Missing new-parent metadata fails with 422; metadata for an existing parent fails with 409.
- Invalid card/category references, content and currency produce documented errors with item indexes where applicable.
- Any failed request leaves no request-created statement, transaction, merchant or alias rows, including late failures, parent conflicts and normalization uniqueness conflicts. No response reports rows discarded by internal rollback.
- Category precedence, legacy fallback, uncategorized confidence and deterministic merchant identity match for single/batch paths.
- The migration preserves existing data and PDF provenance, permits nullable API files and multiple null hashes, and retains non-null per-card hash uniqueness. Unsafe downgrade fails without data loss.
- Existing PDF upload still saves files and populates file metadata, retains `source=pdf`, and preserves deduplication/lifecycle behavior. No PDF-storage removal, reconciliation, idempotency or recurring trigger is introduced.
- Later implementation records strict-TDD evidence and real PostgreSQL migration/integration results; skipped database tests are not proof of atomicity or migration safety.

## Future direction

Separate changes may address PDF-storage removal or reconciliation of API-created and later PDF statements. Reconciliation must define matching tolerances, ambiguous-match review, duplicate handling and preservation of user edits. Parent source alone cannot identify transaction origin, especially for API additions to PDF parents. Do not infer matching or deduplication from `raw_json`, periods or the new source field in this slice.

## Evidence and next step

Read [exploration.md](exploration.md) as historical exploration, not the final scope: its no-migration/existing-parent-only recommendation is superseded here. Project standards are in `openspec/config.yaml`. Current `app/models/statement.py` confirms required file fields and the per-card hash constraint; `app/schemas/domain.py` requires string file fields in `StatementResponse`; `app/services/ingestion.py` constructs PDF parents with path/hash and pending status. These explain why model, migration, schema and response changes are mandatory and why the confirmed API `completed` lifecycle must be assigned explicitly rather than inherited from PDF defaults.

Next: update this change's specifications/design/tasks with the confirmed lifecycle, ID-only existing-parent contract and concurrent-conflict retry rule, then re-forecast the existing feature-branch chain before further implementation. No product decisions remain open in this proposal. Existing uncommitted PR1 code has not been assessed or modified in this proposal revision. Only this proposal is updated; source code and legacy changes remain untouched.

Skill resolution: `paths-injected` (both requested skills loaded). Engram tools are unavailable in this session; decisions and findings are persisted in this OpenSpec artifact instead, with no Engram persistence claimed.
