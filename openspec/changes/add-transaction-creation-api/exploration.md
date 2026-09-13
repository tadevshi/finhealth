# Exploration: add-transaction-creation-api

Change: `add-transaction-creation-api`
Goal: add public API endpoints for individual and batch transaction creation as a backup to PDF ingestion.

## Current state

- `app/api/v1/transactions.py` exposes `GET /api/v1/transactions` and `PATCH /api/v1/transactions/{transaction_id}`; no POST route exists.
- `app/schemas/domain.py` already defines `TransactionCreate`, but it is unused. It requires `statement_id`, date, description, amount, currency, optional free-form category, installment fields, and `raw_json`; `extra="forbid"` is enabled.
- `Transaction.statement_id` is a non-nullable foreign key to `statements.id`. Existing statements are created by PDF ingestion and require card, period, file path, and file hash.
- `Transaction` stores signed `Numeric(15,2)` amounts, 3-character currency, optional category/category_id, merchant_id, low-confidence flag, installment fields, and raw JSON. There is no transaction uniqueness constraint.
- `IngestionService._build_transactions` is the canonical conversion path for PDF rows: amount/date parsing, currency checks, category resolution, merchant normalization, and transaction construction. It persists batches with one commit.
- `MerchantNormalizer.resolve_merchant` and the PATCH category conventions are reusable. Manual creation must avoid divergent tagging behavior.
- Existing HTTP conventions use 400 for invalid query/business input, 404 for missing UUIDs, 422 for content validation, and 201 for successful creation.
- Tests use pytest-asyncio, httpx ASGITransport, and PostgreSQL-backed fixtures; integration tests skip unless PostgreSQL test settings are present.

## Implementation options

### Option A — require an existing `statement_id` (recommended first slice)
Add single and batch POST routes to the existing transactions router. Require a valid statement, resolve category/merchant using existing conventions, and insert atomically. No migration.

Pros: smallest blast radius, preserves the current aggregate-root model, fits the review budget. Cons: callers need an existing statement, which limits truly statement-less manual entry.

### Option B — allow statement-less transactions
Make `statement_id` nullable and audit dashboards, queries, ingestion, and recurring logic. Requires migration and broad behavior changes.

### Option C — synthesize a manual statement
Auto-create a placeholder statement per card/month. Preserves the FK but invents file/hash semantics and changes statement UX and deduplication behavior.

### Option D — extract shared transaction factory
Move category/merchant normalization from ingestion into a shared service. Improves consistency but touches the stable ingestion path and increases risk.

## Open product decisions

1. Require an existing `statement_id` (A), nullable linkage (B), or synthetic manual statements (C)?
2. Accept both `category_id` and legacy free-form `category`, mirroring PATCH, or only closed-set IDs?
3. Resolve merchants automatically with `MerchantNormalizer`, or leave manual rows unlinked?
4. Include idempotency keys now, or document retry-duplicate behavior for the first slice?
5. Batch semantics: all-or-nothing single commit (recommended, matching ingestion) or partial success?
6. Batch response: 201 with `{transactions, count}` (recommended) or multi-status?
7. Currency rule: must match the parent card/statement currency (recommended) or only validate CLP/USD?
8. Run recurring detection for manually created rows now or defer it?

## Recommended first slice

- `POST /api/v1/transactions`: single transaction, existing `statement_id`, optional `category_id`, existing category/merchant conventions, 201 `TransactionResponse`.
- `POST /api/v1/transactions/batch`: 1–200 items, same validation, all-or-nothing transaction, 201 `{transactions, count}`. Invalid rows report the index and persist nothing.
- Reuse the existing transactions router; do not add a new router module.
- Add strict-TDD tests for success, missing statement/category, category confidence behavior, currency mismatch, merchant normalization, batch success, atomicity, and size cap.
- Defer statement-less creation, idempotency keys, recurring detection changes, PATCH changes, dashboard changes, and migrations.

## Risks and mitigations

| Risk | Mitigation |
|---|---|
| Non-null statement FK blocks true manual entry | Decide linkage explicitly; ship Option A first or scope migration separately |
| Normalization drift | Reuse existing category and merchant services; avoid duplicating rules |
| Retry duplicates | Document limitation and defer idempotency key design |
| Manual rows differ in recurring analytics | Keep recurring wiring explicitly out of scope and document it |
| Unbounded batch payload | Enforce 1–200 items in the request schema |
| Review budget exceeds 400 lines | Keep extraction deferred and re-forecast before apply if tasks grow |

## Handoff

Proceed to proposal after confirming the statement/card linkage and the recommended all-or-nothing batch semantics. Artifact language is English; legacy OpenSpec changes remain immutable.
