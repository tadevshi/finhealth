# Proposal: Tolerate Partial Chunk Failures During LLM Ingestion

## Intent

`_run_chunked_extraction` aborts ingestion on any single chunk failure — one bad chunk
discards the rest of a multi-chunk PDF. Small local models occasionally choke on one
section but read the rest fine. Stabilizing this before Phase 2 (more LLM calls) prevents
cascading data loss.

## Scope

### In Scope

- **`app/services/ingestion.py:474-527`** — `_run_chunked_extraction`: replace per-chunk
  `raise` with `logger.warning` + `continue`; add `successful_chunks`/`failed_chunks`/
  `last_chunk_exc` counters; raise `IngestionError` only when ALL chunks fail; emit
  `logger.info` summary.
- **`tests/test_ingestion.py:841-916`** — four scenarios: partial failure tolerated,
  single-chunk failure still raises, all-chunks-fail raises with distinct message,
  log output verified.

### Out of Scope

- Metadata strategy change (replacing `_metadata_completeness`) — conflicts with PR #28.
- `UNKNOWN-` placeholder removal — same conflict.
- `_drop_empty_transactions` move between LLM clients — unnecessary dedup.
- Branch `fix/zen-max-tokens` (`7a79364`) — separate major revert.
- Phase 2 classification — separate SDD change.

## Capabilities

### New Capabilities

- `ingestion-chunk-failure-tolerance`: partial-success for chunked LLM extraction.
  Individual chunk failures logged + counted; ingestion proceeds with remaining chunks.
  Fails only when zero chunks succeed.

### Modified Capabilities

None.

## Approach

Cherry-pick tolerance logic from `fix/tolerant-chunked-extraction` (`5d07757`), discarding
metadata-strategy and placeholder-removal hunks. In the chunk loop: catch `Exception`,
log warning, increment `failed_chunks`, store `last_chunk_exc`, `continue`. After loop:
if `successful_chunks == 0`, raise `IngestionError("LLM extraction failed on all N chunks")`
chaining `last_chunk_exc` via `from`. Keep `_metadata_completeness` selection intact.

## Affected Areas

| Area | Impact | Description |
|------|--------|-------------|
| `app/services/ingestion.py:474-527` | Modified | Chunk loop + post-loop guards |
| `tests/test_ingestion.py:841-916` | Modified + Added | Fail-fast→partial-success test, new all-fail test |

## Risks

| Risk | Likelihood | Mitigation |
|------|------------|------------|
| Cherry-pick accidentally pulls metadata-strategy hunks | Med | Isolate only the chunk-loop + guard changes; review `git diff main` before commit |
| All-fail message confused with single-chunk warning in logs | Low | Distinct messages: "Chunk X/N failed" (warning) vs. "failed on all N chunks" (error) |
| `last_chunk_exc` loses traceback | Low | `from` preserves `__cause__`; test asserts `isinstance(__cause__, LLMExtractionError)` |

## Rollback Plan

Revert the commit. No DB migration, no API surface change. Old fail-fast test preserved
(renamed, not deleted).

## Dependencies

- PR #28 (`efea8cb`) on main — `_metadata_completeness` must remain untouched.

## Success Criteria

- [ ] Chunk failure in multi-chunk PDF does NOT abort; remaining transactions persisted
- [ ] Single-chunk failure still raises `IngestionError`
- [ ] All-chunks-fail raises `IngestionError` with distinct message
- [ ] 284 existing tests + new tolerance tests pass; coverage ≥ 91%
- [ ] `ruff check` + `mypy` clean
