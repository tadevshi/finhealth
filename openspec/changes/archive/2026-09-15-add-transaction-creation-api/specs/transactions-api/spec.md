# Transactions API Specification

## Purpose

The transactions API MUST allow clients to append known transaction rows to existing or request-created statements as a bounded JSON recovery path when PDF ingestion is incomplete or unusable. This capability MUST preserve mandatory statement ownership for every transaction and MUST NOT introduce statement-less transactions, synthetic placeholder statements, reconciliation, deduplication, or recurring-detection behavior.

## Requirements

### Requirement: Single Transaction Creation

The system MUST expose a JSON creation endpoint for a single transaction that requires valid transaction content and a non-null `statement_id`, resolves that statement either as an existing parent or as an atomically created parent with supplied metadata, and returns the created transaction using the existing transaction response shape.

#### Scenario: Create one transaction linked to an existing statement

- GIVEN an existing statement whose parent card has currency `CLP`
- AND a valid JSON transaction with that statement's `statement_id`, no nested statement metadata, date, description, signed decimal amount, and currency `CLP`
- WHEN the client posts it to the single transaction creation endpoint
- THEN the system MUST persist exactly one transaction linked to that statement
- AND the response MUST have HTTP status 201
- AND the response body MUST use the existing `TransactionResponse` fields, including the created transaction `id` and supplied statement linkage
- AND existing transaction read endpoints MUST be able to return the created transaction without invoking PDF ingestion or an LLM

#### Scenario: Create one transaction with a new statement

- GIVEN no statement exists for the supplied `statement_id`
- AND the request includes one nested statement metadata object with valid `credit_card_id`, `period_start`, `period_end`, and `statement_date`
- AND the referenced card exists and has currency `USD`
- AND the transaction currency is `USD`
- WHEN the client posts the request
- THEN the system MUST create the statement under the supplied `statement_id`
- AND the system MUST persist exactly one transaction linked to that new statement
- AND the response MUST have HTTP status 201
- AND the response body MUST use the existing `TransactionResponse` shape

#### Scenario: Reject unknown or invalid input fields

- GIVEN a single creation request containing an unknown field or invalid schema value
- WHEN the client posts the request
- THEN the system MUST reject the request with HTTP status 422
- AND the system MUST NOT persist a transaction or request-created statement for that request

### Requirement: Batch Transaction Creation

The system MUST expose a JSON batch creation endpoint that accepts an object containing 1 to 200 transaction items, validates each item independently against its own resolved parent statement, commits all valid request-created statements and transactions together, and returns created transactions in input order.

#### Scenario: Create multiple transactions in input order

- GIVEN two existing statements that may belong to different cards
- AND a valid batch request with transactions linked to those statements by `statement_id` only
- WHEN the client posts the batch request
- THEN the system MUST persist all submitted transactions
- AND the response MUST have HTTP status 201
- AND the response body MUST contain `transactions` in the same order as the input items
- AND the response body MUST contain `count` equal to the number of committed transactions

#### Scenario: Create mixed batch with existing and new statements

- GIVEN a batch request containing items for multiple statement IDs
- AND some statement IDs already exist and are referenced without nested metadata
- AND one missing statement ID has exactly one item containing required nested statement metadata
- WHEN the client posts the batch request
- THEN each item MUST be validated against its own resolved parent statement and card
- AND the system MUST create the missing statement atomically with all batch transactions
- AND the response transactions MUST preserve input order

#### Scenario: Create maximum-size batch

- GIVEN a valid batch request containing 200 transactions
- WHEN the client posts the batch request
- THEN the system MUST accept the item count bound
- AND the response `count` MUST be 200

#### Scenario: Reject out-of-bounds batch sizes

- GIVEN a batch request containing zero transactions or more than 200 transactions
- WHEN the client posts the batch request
- THEN the system MUST reject the request with HTTP status 422
- AND the system MUST NOT persist any statement, transaction, merchant, or alias rows for that request

### Requirement: Statement Linkage and New Statement Metadata

Every created transaction MUST include a non-null `statement_id`. If that ID identifies an existing statement, the request MUST omit nested statement metadata. If that ID does not identify an existing statement, the request MUST include exactly one nested metadata object for that statement ID containing `credit_card_id`, `period_start`, `period_end`, and `statement_date`; the system MUST validate the card and metadata before writing dependent transactions.

#### Scenario: Existing statement is referenced by ID only

- GIVEN an existing statement
- AND a creation request whose transaction uses that statement's `statement_id`
- AND the request omits nested statement metadata for that statement
- WHEN the client posts the request
- THEN the system MUST use the existing statement as the parent
- AND the system MUST NOT change the existing statement's metadata, source, status, file fields, or errors

#### Scenario: Metadata for an existing statement is rejected as a conflict

- GIVEN an existing statement
- AND a creation request whose transaction uses that statement's `statement_id`
- AND the request includes nested statement metadata for that statement
- WHEN the client posts the request
- THEN the system MUST reject the request with HTTP status 409 even if the metadata matches the existing statement
- AND the system MUST NOT persist any request-created statement, transaction, merchant, or alias rows
- AND the system MUST NOT overwrite existing statement metadata

#### Scenario: Missing statement requires metadata

- GIVEN no statement exists for the supplied `statement_id`
- AND a creation request uses that `statement_id` without nested statement metadata
- WHEN the client posts the request
- THEN the system MUST reject the request with HTTP status 422
- AND the system MUST NOT persist a transaction for that item or request

#### Scenario: New statement metadata validates required fields

- GIVEN no statement exists for the supplied `statement_id`
- AND the request includes nested statement metadata missing `credit_card_id`, `period_start`, `period_end`, or `statement_date`
- WHEN the client posts the request
- THEN the system MUST reject the request with HTTP status 422
- AND the system MUST NOT persist the statement or any dependent transaction

#### Scenario: Unknown card is rejected

- GIVEN no statement exists for the supplied `statement_id`
- AND the request includes nested statement metadata with a `credit_card_id` that does not identify an existing card
- WHEN the client posts the request
- THEN the system MUST reject the request with HTTP status 404
- AND the system MUST NOT persist the statement or any dependent transaction

#### Scenario: Invalid statement period is rejected

- GIVEN no statement exists for the supplied `statement_id`
- AND the request includes nested statement metadata where `period_start` is after `period_end`
- WHEN the client posts the request
- THEN the system MUST reject the request with HTTP status 422
- AND the system MUST NOT invent alternative statement dates or period bounds

### Requirement: API-Created Statement Persistence

A statement created through transaction creation MUST be stored under the caller-supplied `statement_id`, MUST use the supplied `credit_card_id`, `period_start`, `period_end`, and `statement_date`, MUST start with `status=completed`, MUST have nullable `file_path` and `file_hash` stored as null, and MUST have explicit source/provenance `api`. The system MUST NOT schedule PDF ingestion, recurring detection, or file work for API-created statements.

#### Scenario: New statement has API provenance and no file metadata

- GIVEN a valid creation request for a missing `statement_id` with required nested statement metadata
- WHEN the request succeeds
- THEN the created statement MUST have source `api`
- AND the created statement MUST have status `completed`
- AND the created statement's `file_path` MUST be null
- AND the created statement's `file_hash` MUST be null
- AND no PDF parsing, file-saving, hashing, ingestion job, or recurring job MUST be scheduled by this request

#### Scenario: API completed status describes creation completion only

- GIVEN a statement created by the transaction creation API
- WHEN clients read that statement
- THEN the statement status MUST be `completed`
- AND the system MUST NOT imply that PDF extraction ran or that the statement contains all financial activity for the period

### Requirement: Shared Batch Statement Metadata

For a batch request, repeated items for one new `statement_id` MUST share one authoritative nested statement metadata object. The metadata-bearing item MAY appear anywhere in input order. Additional non-null metadata objects for the same new `statement_id` MUST be rejected with HTTP 422, including when they are identical; conflicting metadata for the same ID MUST also be rejected with HTTP 422.

#### Scenario: Repeated batch items share one metadata object

- GIVEN no statement exists for a supplied `statement_id`
- AND a batch contains multiple transaction items using that `statement_id`
- AND exactly one of those items supplies required nested statement metadata
- WHEN the client posts the batch request
- THEN the system MUST resolve that metadata for all items with the same `statement_id`
- AND the metadata-bearing item MAY appear before or after other items for that statement
- AND all transactions for that ID MUST link to the same request-created statement

#### Scenario: Duplicate metadata objects for one new statement are rejected

- GIVEN no statement exists for a supplied `statement_id`
- AND a batch contains more than one item with non-null nested statement metadata for that same `statement_id`
- WHEN the client posts the batch request
- THEN the system MUST reject the request with HTTP status 422
- AND the error SHOULD identify the zero-based index of the additional metadata object
- AND the system MUST NOT persist any writes from the batch

#### Scenario: Conflicting metadata for one new statement is rejected

- GIVEN no statement exists for a supplied `statement_id`
- AND a batch contains multiple metadata objects for that `statement_id` with conflicting values
- WHEN the client posts the batch request
- THEN the system MUST reject the request with HTTP status 422
- AND the system MUST NOT choose one metadata object or merge the values

### Requirement: Concurrent New-Statement Creation Conflicts

If concurrent requests attempt to create the same missing statement ID, exactly one request MAY succeed. A losing request MUST fail atomically with HTTP 409 and MUST NOT silently reuse the concurrently created parent or automatically retry. The client retry path MUST be an explicit later request using ID-only linkage and omitting nested metadata for that now-existing statement.

#### Scenario: Concurrent creation has one winner and one conflict

- GIVEN two otherwise valid requests attempt to create the same missing `statement_id` with nested statement metadata at the same time
- WHEN the requests race to commit
- THEN one request MAY succeed with HTTP 201
- AND the losing request MUST fail with HTTP status 409
- AND the losing request MUST NOT persist any request-created statement, transaction, merchant, or alias rows

#### Scenario: Losing client retries with ID-only linkage

- GIVEN a request lost a concurrent new-statement creation race and received HTTP 409
- AND the winning request's statement now exists
- WHEN the losing client retries using the existing `statement_id` and omits nested statement metadata
- THEN the system MAY create the transaction linked to the existing statement if all other validation passes
- AND the system MUST NOT require or accept the previous creation metadata on the retry

### Requirement: Category and Category ID Semantics

The system MUST support both canonical `category_id` input and legacy free-form `category` input. A supplied non-null `category_id` MUST take precedence over `category`, MUST write the canonical category name, and MUST mark the transaction as not low confidence. Without `category_id`, a supplied free-form `category` MUST be preserved without resolving it to a category ID and MUST be marked low confidence. If neither category field is supplied, both category fields MUST remain null and the transaction MUST be marked low confidence.

#### Scenario: Category ID takes precedence

- GIVEN an existing category with canonical name `Groceries`
- AND a creation request containing that category's `category_id`
- AND the request also contains a different free-form `category` value
- WHEN the client posts the request
- THEN the created transaction MUST reference the supplied `category_id`
- AND the created transaction MUST store the canonical category name `Groceries`
- AND the created transaction MUST have `low_confidence` set to false

#### Scenario: Unknown category ID is rejected

- GIVEN a creation request containing a `category_id` that does not identify an existing category
- WHEN the client posts the request
- THEN the system MUST reject the request with HTTP status 404
- AND the system MUST NOT persist any transaction for that item or request

#### Scenario: Legacy category string is preserved

- GIVEN a creation request with no `category_id`
- AND the request contains a free-form `category` value
- WHEN the client posts the request
- THEN the created transaction MUST store the supplied category string
- AND the created transaction's category ID MUST remain null
- AND the created transaction MUST have `low_confidence` set to true
- AND the system MUST NOT silently resolve the category string to a category ID

#### Scenario: No category input remains uncategorized

- GIVEN a creation request with neither `category_id` nor `category`
- WHEN the client posts the request
- THEN the created transaction's category and category ID MUST both be null
- AND the created transaction MUST have `low_confidence` set to true

### Requirement: Currency Validation Against Parent Card

The system MUST require each created transaction to use an exact supported currency code matching the currency of the resolved parent statement's credit card, including for request-created statements. The system MUST NOT infer currency from statements, convert amounts, or accept unsupported currency codes.

#### Scenario: Matching supported currency is accepted

- GIVEN an existing statement whose parent card currency is `USD`
- AND a valid creation request with currency `USD`
- WHEN the client posts the request
- THEN the system MUST accept the currency value
- AND the created transaction MUST store currency `USD`

#### Scenario: New statement currency is checked against its card

- GIVEN no statement exists for the supplied `statement_id`
- AND the nested statement metadata references a card with currency `CLP`
- AND the transaction currency is `CLP`
- WHEN the client posts the request
- THEN the system MUST accept the currency value
- AND the created transaction MUST store currency `CLP`

#### Scenario: Currency mismatch is rejected

- GIVEN a resolved parent statement whose parent card currency is `CLP`
- AND a valid creation request with currency `USD`
- WHEN the client posts the request
- THEN the system MUST reject the request with HTTP status 400
- AND the system MUST NOT persist any transaction for that item or request

#### Scenario: Unsupported currency is rejected

- GIVEN a creation request with a currency other than supported `CLP` or `USD`
- WHEN the client posts the request
- THEN the system MUST reject the request with HTTP status 422
- AND the system MUST NOT persist any transaction for that item or request

### Requirement: Deterministic Merchant Handling

The system MUST use deterministic merchant normalization, including existing merchant and alias lookup and deterministic merchant or alias creation. The system MUST NOT invoke optional LLM merchant resolution. Merchant handling MUST NOT override explicit or absent category input for this capability.

#### Scenario: Existing merchant alias is reused

- GIVEN an existing merchant alias matching the transaction description under deterministic normalization
- AND a valid creation request with that description
- WHEN the client posts the request
- THEN the created transaction MUST reference the merchant resolved by the existing alias
- AND the system MUST NOT call an LLM to determine merchant identity

#### Scenario: Unresolved merchant remains null

- GIVEN a valid creation request whose description cannot be resolved to a merchant by deterministic rules
- WHEN the client posts the request
- THEN the transaction MAY be created with a null merchant reference
- AND category and confidence fields MUST follow the category input rules for this capability

### Requirement: Request Atomicity

Each creation request MUST be atomic across request-created statements, transaction rows, and any merchant or alias rows created by deterministic normalization. The system MUST either persist all writes for the request or persist none of them, and MUST NOT return partial success.

#### Scenario: Batch failure rolls back earlier processed rows

- GIVEN a batch request whose first item is valid
- AND a later item fails validation or persistence
- WHEN the client posts the batch request
- THEN the system MUST reject the request
- AND the system MUST NOT persist the first transaction
- AND the system MUST NOT persist any request-created statement, merchant, or alias rows created while processing the request

#### Scenario: Database failure never produces partial 201

- GIVEN a creation request that encounters a database failure before the request is fully committed
- WHEN the system responds
- THEN the response MUST NOT be HTTP 201
- AND no partial request-created statement, transaction, merchant, or alias rows MUST remain persisted

#### Scenario: Parent conflict rolls back all request writes

- GIVEN a request includes valid transaction, merchant, or new-statement work
- AND the request later fails because statement metadata conflicts with an existing or concurrently created statement
- WHEN the system responds with HTTP 409
- THEN no request-created statement, transaction, merchant, or alias rows MUST remain persisted

### Requirement: Error Reporting and Bounds

The system MUST use consistent HTTP error classes for creation failures: 422 for schema or content validation, missing new-parent metadata, invalid new-parent metadata, additional metadata objects for one new statement, and batch bounds; 409 for metadata supplied for an existing statement and concurrent parent-creation conflicts; 404 for unknown card or category UUID references; and 400 for currency business-rule mismatches. Batch item failures MUST identify the zero-based input index when the failure belongs to a specific item.

#### Scenario: Indexed batch validation error

- GIVEN a batch request where item index 1 references a missing category
- WHEN the client posts the batch request
- THEN the system MUST reject the request with HTTP status 404
- AND the error response SHOULD identify index 1 as the failing input item
- AND the system MUST NOT persist any writes from the batch

#### Scenario: Indexed duplicate metadata error

- GIVEN a batch request where item index 2 supplies an additional metadata object for a new statement already defined by another item
- WHEN the client posts the batch request
- THEN the system MUST reject the request with HTTP status 422
- AND the error response SHOULD identify index 2 as the failing input item
- AND the system MUST NOT persist any writes from the batch

#### Scenario: Monetary values retain decimal semantics

- GIVEN a creation request with a valid signed decimal amount
- WHEN the client posts the request
- THEN the system MUST store the amount using decimal monetary semantics compatible with `Numeric(15,2)`
- AND the system MUST NOT require or expose floating-point arithmetic behavior

### Requirement: Statement Model and Migration Compatibility

The system MUST add model, schema, and migration compatibility for file-free API-created statements while preserving existing PDF statements. `Statement.file_path` and `Statement.file_hash` MUST become nullable, statement source MUST be explicit and non-null with supported values `pdf` and `api`, existing statements MUST be backfilled as `pdf`, and existing non-null file metadata MUST be preserved. The migration and application defaults MUST preserve safe PDF behavior for existing creation paths.

#### Scenario: Existing PDF statements are backfilled and preserved

- GIVEN existing statements with file paths and file hashes before the migration
- WHEN the migration is applied
- THEN those statements MUST retain their existing `file_path` and `file_hash` values
- AND those statements MUST have source `pdf`
- AND the migration MUST NOT rewrite, delete, or move stored PDF files

#### Scenario: API statements allow nullable file metadata

- GIVEN the migration has been applied
- WHEN an API-created statement is stored
- THEN the database and model MUST allow null `file_path` and null `file_hash`
- AND the statement source MUST be `api`

#### Scenario: Unsafe downgrade refuses incompatible data

- GIVEN API-created statements or null statement file metadata exist
- WHEN a downgrade would restore incompatible non-null file constraints or remove source support
- THEN the downgrade MUST fail safely or require explicit preconditions
- AND the system MUST NOT fabricate file paths, fabricate file hashes, delete statements, or discard provenance silently

### Requirement: PDF Statement Behavior Preservation

Existing PDF upload, file-saving, hashing, deduplication, source, and ingestion lifecycle behavior MUST remain unchanged by transaction creation. PDF-created statements MUST retain source `pdf`, non-null file metadata, existing uniqueness behavior for non-null hashes, and existing upload behavior. Removing PDF storage is future scope.

#### Scenario: PDF upload still stores file metadata and source

- GIVEN a valid PDF upload through the existing upload flow
- WHEN the upload creates a statement
- THEN the statement MUST retain source `pdf`
- AND the statement MUST store the file path and file hash according to existing PDF behavior
- AND existing ingestion lifecycle behavior MUST remain unchanged

#### Scenario: Non-null PDF hash uniqueness is preserved

- GIVEN a PDF statement exists for a card with a non-null file hash
- WHEN another PDF upload for the same card produces the same non-null hash
- THEN the existing per-card non-null hash uniqueness behavior MUST still reject the duplicate
- AND the same non-null hash for a different card MUST remain governed by the existing cross-card allowance

#### Scenario: Multiple API statements with null hashes are allowed

- GIVEN two valid API-created statements for the same card have null `file_hash`
- WHEN both are stored under different statement IDs
- THEN the system MUST allow both statements under ordinary nullable unique semantics
- AND the system MUST NOT introduce placeholder hashes or nulls-not-distinct uniqueness for API statements

### Requirement: Response Shape Compatibility

Creation responses MUST preserve existing transaction response compatibility. Single creation MUST return one existing transaction response object. Batch creation MUST return an object containing `transactions` and `count`. Statement responses MUST evolve to expose explicit source and nullable file fields. Existing GET, PATCH, PDF ingestion, and upload behaviors MUST remain compatible except for the intentional statement-response evolution.

#### Scenario: Existing read and patch contracts remain compatible

- GIVEN existing clients using transaction list or category patch behavior
- WHEN the creation API is added
- THEN existing GET and PATCH contracts MUST remain available with their existing transport and response semantics
- AND creation MUST use JSON request bodies rather than PATCH form or HTML transport semantics
- AND creation MUST NOT use PATCH's empty-string category-clear sentinel

#### Scenario: Batch response contains only committed rows

- GIVEN a successful batch creation request
- WHEN the system returns HTTP 201
- THEN every transaction in the response body MUST correspond to a committed row
- AND `count` MUST equal the number of returned and committed transactions

#### Scenario: Statement response exposes source and nullable file fields

- GIVEN clients read an API-created statement
- WHEN the system serializes the statement response
- THEN the response MUST expose source `api`
- AND the response MUST allow `file_path` and `file_hash` to be null

#### Scenario: API transactions appended to PDF statements do not change statement source

- GIVEN an existing PDF statement with source `pdf`
- WHEN a client creates an API transaction linked to that existing statement by ID only
- THEN the statement source MUST remain `pdf`
- AND statement source MUST NOT be treated as transaction-level provenance

### Requirement: Compatibility and Non-Goals

The creation API MUST be append-only, non-idempotent, and statement-linked for this change. The system MUST NOT provide statement-less/manual transaction lifecycle, synthetic placeholder statements, standalone statement creation, idempotency keys, automatic retry deduplication, PDF reconciliation, merge or replacement behavior, recurring detection changes, new authorization models, transaction-level provenance migration, PDF storage removal, or source-of-truth changes to PDF ingestion as part of this capability.

#### Scenario: Duplicate-looking retry is not deduplicated

- GIVEN a client submits the same valid transaction content more than once
- WHEN each request succeeds
- THEN the system MAY create multiple ordinary transaction rows
- AND the system MUST NOT claim idempotent retry protection or automatic duplicate reconciliation

#### Scenario: Future reconciliation remains out of scope

- GIVEN an API-created transaction linked to a statement
- WHEN a later PDF ingestion contains a similar transaction
- THEN this capability MUST NOT define automatic matching, merging, replacement, deletion, or provenance-based reconciliation behavior
- AND callers MUST treat reconciliation as a separate future capability

#### Scenario: Statement-less creation remains out of scope

- GIVEN a client wants to create a transaction without a `statement_id`
- WHEN the client submits a creation request without a valid non-null `statement_id`
- THEN the system MUST reject the request
- AND the system MUST NOT synthesize an unattached transaction

#### Scenario: PDF storage removal remains out of scope

- GIVEN existing PDF statements and stored PDF files
- WHEN this creation API is implemented
- THEN the system MUST NOT remove PDF storage, delete existing PDFs, or change upload file-saving behavior
- AND any such cleanup MUST require a separate future change
