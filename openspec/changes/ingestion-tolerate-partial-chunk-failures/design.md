# Design: Tolerate Partial Chunk Failures During LLM Ingestion

## Context

`_run_chunked_extraction` in `app/services/ingestion.py:423-527` aborts the entire ingestion when any single chunk fails. Small local models (qwen2.5:1.5b) occasionally choke on one section but parse the rest correctly — one bad chunk should not discard the entire PDF.

This change cherry-picks ONLY the chunk-failure-tolerance logic from local branch `fix/tolerant-chunked-extraction` (commit `5d07757`), discarding its metadata-strategy replacement and placeholder-removal hunks. PR #28 (`_metadata_completeness`, lines 820-847 on main) is a regression-guard: it stays untouched.

The change is ~50-80 LOC across two files: the chunk loop in `ingestion.py` and new test scenarios in `test_ingestion.py`.

## Goals & Non-Goals

**Goals:**
- Tolerate individual chunk failures in multi-chunk PDFs; persist surviving transactions
- Raise `IngestionError` only when ALL chunks fail, with distinct message and `__cause__` chain
- Emit `logger.warning` per failed chunk and `logger.info` summary on every run (success, partial, all-fail)
- Keep `_metadata_completeness` selection algorithm unchanged (PR #28 regression-guard)

**Non-Goals:**
- Metadata strategy change (replacing `_metadata_completeness` with "first cardholder wins")
- `UNKNOWN-{hash}` placeholder removal
- `ExceptionGroup` upgrade (stay with `__cause__`)
- Phase 2 classification, Zen config, branch `fix/zen-max-tokens`

## Approach

### Code Structure

Three counters are declared before the loop, alongside the existing accumulators (line 469-472 on main):

```python
successful_chunks: int = 0
failed_chunks: int = 0
last_chunk_exc: Exception | None = None
```

The loop body replaces the fail-fast `except` block with a tolerate-and-continue pattern. The `isinstance(exc, IngestionError): raise` branch is **removed** — all exceptions are treated as chunk failures (see Decision 5 below).

After the loop, a `try/finally` block ensures the summary log always fires:

```python
try:
    # All-fail guard
    if successful_chunks == 0 and failed_chunks > 0:
        raise IngestionError(
            f"LLM extraction failed on all {len(chunks)} chunks"
        ) from last_chunk_exc

    # Metadata-None guard (existing, with chaining addition)
    if all_metadata is None:
        if failed_chunks > 0:
            raise IngestionError(
                "LLM did not return a usable metadata block in any chunk"
            ) from last_chunk_exc
        raise IngestionError(
            "LLM did not return a usable metadata block in any chunk"
        )

    deduped = _dedupe_transactions(all_transactions)
    return ExtractionResponse(
        transactions=deduped,
        metadata=all_metadata,
        confidence=first_confidence,
        notes=first_notes,
    )
finally:
    logger.info(
        "Chunked extraction complete: %d successful, %d failed, %d transactions",
        successful_chunks,
        failed_chunks,
        len(all_transactions),
    )
```

### Order of Operations After the Loop

1. All-fail guard (`successful_chunks == 0`)
2. Metadata-None guard (`all_metadata is None`)
3. Dedup (`_dedupe_transactions`)
4. Return `ExtractionResponse`
5. Summary log (in `finally` — fires on all paths including raises)

## Decisions

### Decision 1: Summary Log Always Emitted via `try/finally`

| Option | Tradeoff | Decision |
|--------|----------|----------|
| (A) Reorder: guard → log → raise | Two raise sites; summary duplicated or awkward | Rejected |
| (B) `try/finally` | Single emit point, clean semantics, no duplication | **Chosen** |
| (C) Counters + wrapper | Unusual pattern, harder to read | Rejected |
| (D) Relax spec | Punts decision back, contradicts user intent | Rejected |

**Rationale**: `try/finally` guarantees the summary fires on success (before return), partial (before return), and all-fail (before exception propagates). Single emit point, no code duplication.

### Decision 2: Stay with `__cause__` (No `ExceptionGroup`)

| Option | Tradeoff | Decision |
|--------|----------|----------|
| Stay with `__cause__` | Only last chunk's exception preserved; matches spec and branch | **Chosen** |
| Upgrade to `ExceptionGroup` | Semantically correct but requires spec change, Python 3.11+ feature | Rejected |

**Rationale**: The spec is written for `__cause__`. All-fail typically has the same root cause on every chunk. `ExceptionGroup` would require spec amendment and adds complexity for no practical benefit.

### Decision 3: `caplog` for Log Capture in Tests

| Option | Tradeoff | Decision |
|--------|----------|----------|
| `caplog` with `set_level` | Standard pytest fixture, well-known, minimal setup | **Chosen** |
| Custom recorder on fakes | More invasive, isolates from logging config | Rejected |
| Assert on side-effects only | "Exactly one warning" becomes implicit | Rejected |

**Rationale**: `caplog` is the standard pytest approach. Requires `caplog.set_level(logging.WARNING, logger="app.services.ingestion")` per test. No new infrastructure needed.

### Decision 4: Metadata-None Guard Is Reachable — Keep It

**Analysis**: `ExtractionResponse.metadata` is a required `StatementMetadata` field (not `Optional`). A successful chunk always produces a non-None metadata object. However, `_metadata_completeness` returns 0 when all 6 fields are empty strings, and `_metadata_completeness(None)` also returns 0. So `all_metadata` stays `None` if every successful chunk returns metadata with ALL fields empty.

**Verdict**: Technically reachable (degenerate case: LLM returns transactions but no metadata fields). Keep the guard. Chain `from last_chunk_exc` only when `failed_chunks > 0` (there's an exception to chain). When `failed_chunks == 0` but metadata is empty, raise without chaining.

### Decision 5: Tolerate ALL Exceptions Including `IngestionError`

The `isinstance(exc, IngestionError): raise` branch on main is **removed**. All exceptions from the LLM client are treated as chunk failures.

**Rationale**: The spec's intent is "any chunk failure is a chunk failure." An `IngestionError` from the LLM client would indicate double-wrapping; tolerating it simplifies the loop and matches the spec's scenario "Non-typed exception is also tolerated."

### Decision 6: Log Message Strings (Stable for Test Assertions)

- **Per-chunk warning**: `"Chunk %d/%d failed: %s. Continuing with remaining chunks."`
- **Summary info**: `"Chunked extraction complete: %d successful, %d failed, %d transactions"`
- **All-fail error**: `"LLM extraction failed on all %d chunks"`

### Decision 7: `_metadata_completeness` Regression Guard

The function at `app/services/ingestion.py:820-847` (main) stays **untouched**. The metadata selection call at line 509 (`_metadata_completeness(response.metadata) > _metadata_completeness(all_metadata)`) is unchanged. The branch's replacement ("first cardholder wins") is explicitly discarded.

## File Changes

| File | Action | Description | ~LOC |
|------|--------|-------------|------|
| `app/services/ingestion.py:469-527` | Modify | Add counters, replace except block, add try/finally with guards and summary log | ~40 |
| `tests/test_ingestion.py` | Modify + Add | Rename existing fail-fast test, add partial-success and all-fail tests with caplog | ~80 |

**No other files change.** No DB migration. No API surface change. No new dependencies.

## Testing Strategy

### Log Capture

Use `caplog.set_level(logging.WARNING, logger="app.services.ingestion")` in each test that asserts log output. Assert `len([r for r in caplog.records if r.levelno == logging.WARNING])` for "exactly one warning."

### Scenario-to-Test Mapping

| # | Spec Scenario | Test Function | Class |
|---|---------------|---------------|-------|
| 1 | One bad chunk in 3-chunk PDF | `test_partial_chunk_failure_tolerated` | `TestIngestStatementChunked` |
| 2 | Non-typed exception tolerated | `test_non_typed_exception_tolerated` | `TestIngestStatementChunked` |
| 3 | 1-chunk PDF, LLM error | `test_single_chunk_failure_still_raises` (rename existing `test_chunk_failure_fails_whole_ingestion`) | `TestIngestStatementChunked` |
| 4 | Every chunk in 3-chunk PDF raises | `test_all_chunks_fail_raises` | `TestIngestStatementChunked` |
| 5 | Partial-failure logs summary | `test_partial_chunk_failure_tolerated` (combined with #1) | `TestIngestStatementChunked` |
| 6 | All-fail preserves `__cause__` identity | `test_all_chunks_fail_raises` (combined with #4) | `TestIngestStatementChunked` |
| 7 | Most-complete metadata wins after partial failure | `test_metadata_completeness_wins_after_partial_failure` | `TestIngestStatementChunked` |
| 8 | Summary log on success run | `test_summary_log_on_success` | `TestIngestStatementChunked` |

### Test File Organization

All new tests go in the existing `TestIngestStatementChunked` class in `tests/test_ingestion.py`. The existing `test_chunk_failure_fails_whole_ingestion` is renamed to `test_single_chunk_failure_still_raises` and updated to use a 1-chunk PDF.

### Fake LLM Client Enhancement

A new `_FailOnNthChunk` dataclass (local to the test) replaces the inline `_FailOnSecondChunk`. It accepts `fail_on: set[int]` (0-indexed chunk indices to fail on) and `exc: Exception`, enabling scenarios #1, #2, and #4 with a single helper.

## Migration & Rollback

**No Alembic migration required.** The change is purely behavioral (error handling in the chunk loop). No schema change, no new column, no data migration.

**No API surface change.** No FastAPI route is added or modified. The HTTP layer's `IngestionError → 422` mapping continues to work unchanged.

**Rollback**: Revert the commit. The old fail-fast test is renamed, not deleted, so reverting restores the original test name and behavior.

## Out of Scope

These items from the branch are explicitly discarded:

- **Metadata strategy change**: Branch replaces `_metadata_completeness` with "first cardholder wins" (`if all_metadata is None and response.metadata.cardholder`). This conflicts with PR #28 and is NOT part of this change.
- **`UNKNOWN-{hash}` placeholder removal**: Branch removes the `file_hash_short` fallback for `card_number_masked` and `cardholder`. Conflicts with PR #28's approach.
- **`_metadata_completeness` function deletion**: Branch deletes the function (lines 820-847 on main). This change keeps it.
- **`_drop_empty_transactions` move**: Branch moves dedup between LLM clients. Unnecessary for this change.
- **Branch `fix/zen-max-tokens`**: Separate major revert of PR #26, entirely out of scope.

## Open Questions

None. All 4 spec-flagged risks are resolved above.
