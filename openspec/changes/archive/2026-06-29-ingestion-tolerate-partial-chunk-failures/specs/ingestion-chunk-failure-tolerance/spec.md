# ingestion-chunk-failure-tolerance

## Purpose

Formalise the partial-success semantics of `_run_chunked_extraction` in
`app/services/ingestion.py:423-527`: one bad chunk MUST NOT abort ingestion
of a multi-chunk PDF. Single-chunk uploads and full-failure runs keep
their fail-fast behaviour. Metadata selection (`_metadata_completeness`,
PR #28) is unchanged. Out of scope: `UNKNOWN-{hash}` placeholders, Zen
config, Phase 2.

## ADDED Requirements

### Requirement: Partial Chunk Failure Is Tolerated

The system MUST tolerate a non-`IngestionError` exception raised by the
LLM client on any single chunk of a multi-chunk PDF: log a
`logger.warning` naming the chunk index, increment a failed-chunks
counter, and continue with the remaining chunks. Transactions from the
surviving chunks MUST be persisted; the statement MUST complete with
`status = completed`.

#### Scenario: One bad chunk in a 3-chunk PDF

- GIVEN a PDF chunked into 3+ windows and an LLM client that returns a
  valid `ExtractionResponse` for chunks 1 and 3 but raises
  `LLMExtractionError` on chunk 2
- WHEN `ingest_statement` is invoked
- THEN chunks 1 and 3 transactions are persisted, the statement completes
  with `status = completed`, and exactly one `logger.warning` names
  "Chunk 2/3"

#### Scenario: Non-typed exception is also tolerated

- GIVEN a multi-chunk PDF where one chunk raises a bare `KeyError` (an
  LLM-client bug, not `LLMExtractionError`)
- WHEN extraction runs
- THEN the exception is logged at `warning` level, the loop continues,
  and the statement still completes with `status = completed`

### Requirement: Single-Chunk Upload That Fails Still Raises

When a 1-chunk PDF raises during extraction, the system MUST raise
`IngestionError` wrapping the original cause via `from`; the statement
MUST NOT be persisted. The tolerance MUST NOT regress the single-chunk
fail-fast contract.

#### Scenario: 1-chunk PDF, LLM error

- GIVEN a PDF producing exactly one chunk and an LLM client that raises
  `LLMExtractionError("network timeout")`
- WHEN `ingest_statement` is invoked
- THEN `IngestionError` is raised, its `__cause__` is the
  `LLMExtractionError`, and no `Statement` row is created

### Requirement: All-Chunks-Fail Raises with Distinct Message

When every chunk of a multi-chunk PDF raises, the system MUST raise
`IngestionError` whose message includes "all N chunks" (N = chunk count)
and whose `__cause__` is the last chunk's original exception. The
message MUST differ from the per-chunk warning and from any
single-chunk fail-fast message.

#### Scenario: Every chunk in a 3-chunk PDF raises

- GIVEN a 3-chunk PDF where every chunk raises `LLMExtractionError`
- WHEN extraction runs
- THEN `IngestionError` is raised, its message contains "all 3 chunks",
  and `isinstance(raised.__cause__, LLMExtractionError)` is True

### Requirement: Extraction Summary Log Is Emitted

The system MUST emit exactly one `logger.info` line at the end of
`_run_chunked_extraction` whose payload includes `successful_chunks`,
`failed_chunks`, and `len(all_transactions)` (pre-dedup). The line MUST
be emitted on success, partial, and all-fail runs.

#### Scenario: Partial-failure run logs the summary

- GIVEN a 3-chunk PDF where chunk 2 fails
- WHEN extraction finishes
- THEN a single `logger.info` is emitted with
  `successful_chunks=2, failed_chunks=1, transactions=N` and the persisted
  count reflects only the surviving chunks (after dedup)

### Requirement: Original Exception Preserved on `__cause__`

When `_run_chunked_extraction` raises `IngestionError` for the
all-chunks-fail case, `__cause__` MUST be the actual exception instance
(not a stringified form), so log readers can introspect the root cause's
type, message, and traceback.

#### Scenario: All-fail preserves LLMExtractionError identity

- GIVEN a multi-chunk PDF where every chunk raises
  `LLMExtractionError("upstream timeout")`
- WHEN the all-fail guard raises
- THEN `isinstance(raised.__cause__, LLMExtractionError)` is True and
  `"upstream timeout" in str(raised.__cause__)` is True

### Requirement: Metadata Selection Algorithm Unchanged

The system MUST keep selecting the canonical `StatementMetadata` via
`_metadata_completeness(response.metadata) > _metadata_completeness(all_metadata)`
(PR #28). The tolerance MUST NOT introduce a "first cardholder wins" rule
or any other alternative selection strategy.

#### Scenario: Most-complete metadata still wins after partial failure

- GIVEN a 3-chunk PDF where chunk 1 returns metadata with only
  `cardholder` populated, chunk 2 fails, and chunk 3 returns metadata
  with all six fields populated
- WHEN extraction runs
- THEN the canonical metadata on the statement row is chunk 3's (most
  complete), not chunk 1's
