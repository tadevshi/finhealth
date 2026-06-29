# Tasks: Tolerate Partial Chunk Failures During LLM Ingestion

## Summary

- **~120 LOC** (40 prod + 80 test), **13 work units**, single PR. Tests: 284 + 7 = **291**.
- **Source**: cherry-pick from `fix/tolerant-chunked-extraction` (`5d07757`). KEEP chunk-loop + post-loop guards. DISCARD "first cardholder wins" replacement, `_metadata_completeness` deletion, `UNKNOWN-` placeholder removal, `_drop_empty_transactions` move, `fix/zen-max-tokens`.

## Review Workload Forecast

Decision needed before apply: No
Chained PRs recommended: No
Chain strategy: size-exception
400-line budget risk: Low

## Tasks

### Phase 1 — Production (`app/services/ingestion.py`)

- [ ] **1. Add counters.** In `_run_chunked_extraction` (lines 469-472), declare `successful_chunks: int = 0`, `failed_chunks: int = 0`, `last_chunk_exc: Exception | None = None` alongside accumulators. No behavior change.
- [ ] **2. Rewrite per-chunk `except`.** Drop `isinstance(exc, IngestionError): raise` and `raise IngestionError(...) from exc`. Replace with `logger.warning("Chunk %d/%d failed: %s. Continuing with remaining chunks.", ...)` + `failed_chunks += 1` + `last_chunk_exc = exc` + `continue`. Bump `successful_chunks` on success.
- [ ] **3. All-fail guard + `try/finally` summary.** Wrap existing metadata-None guard + dedup + return in `try/finally`. Top of `try`: `if successful_chunks == 0 and failed_chunks > 0: raise IngestionError(f"LLM extraction failed on all {len(chunks)} chunks") from last_chunk_exc`. `finally`: `logger.info("Chunked extraction complete: %d successful, %d failed, %d transactions", ...)`. Fires on success, partial, all-fail.
- [ ] **4. Chain `last_chunk_exc` on metadata-None guard.** Inside `if all_metadata is None:`, raise `IngestionError("LLM did not return a usable metadata block in any chunk")`; chain `from last_chunk_exc` only when `failed_chunks > 0`.

### Phase 2 — Test helpers (`tests/test_ingestion.py`)

- [ ] **5. Add `_FailOnNthChunk` + chunker mock.** `_FailOnNthChunk(FakeLLMClient)` takes `fail_on: set[int]` and `exc: Exception`; raises when `len(calls) - 1 in fail_on`. Add `_single_chunk(monkeypatch)` patching `app.services.ingestion.chunk_for_llm` to `["only chunk"]` for Task 6.

### Phase 3 — Test scenarios (`tests/test_ingestion.py`)

- [ ] **6. Rename + refit fail-fast test.** `test_chunk_failure_fails_whole_ingestion` → `test_single_chunk_failure_still_raises`. Chunker mock + `_FailOnNthChunk(fail_on={0}, exc=LLMExtractionError("network timeout"))`. Assert `IngestionError`, `isinstance(__cause__, LLMExtractionError)`, no row persisted.
- [ ] **7. `test_partial_chunk_failure_tolerated`.** Real `SANTANDER_PDF`, `fail_on={1}`, `LLMExtractionError("chunk 2 outage")`, `caplog.set_level(logging.WARNING, logger="app.services.ingestion")`. Assert `status == COMPLETED`, `len(transactions) > 0`, one WARNING with `"Chunk 2/"`, one INFO matching `"Chunked extraction complete"`.
- [ ] **8. `test_non_typed_exception_tolerated`.** Same as Task 7 but `exc=KeyError("malformed payload")`. Assert statement completes, one WARNING.
- [ ] **9. `test_all_chunks_fail_raises`.** `FakeLLMClient(raise_exc=LLMExtractionError("upstream timeout"))` raises every call. Assert `IngestionError`, message contains `"all "` + chunk count, `isinstance(__cause__, LLMExtractionError)`, summary INFO in `caplog.records` pre-propagation.
- [ ] **10. `test_metadata_completeness_wins_after_partial_failure`.** 3-chunk PDF, `fail_on={1}`; chunk 1 has only `cardholder`, chunk 3 has all six fields. Assert persisted metadata = chunk 3's (regression-guard for PR #28). `_metadata_completeness` is NOT modified.
- [ ] **11. `test_summary_log_on_success`.** Happy path, `caplog.set_level(logging.INFO, logger="app.services.ingestion")`. Assert one INFO with `"Chunked extraction complete"` and `failed_chunks=0`.

### Phase 4 — Quality gates

- [ ] **12. Suite + lint.** `pytest -q` ≥291 tests, coverage ≥91%. `ruff check .`, `ruff format --check .`, `mypy --strict app/` clean. `git diff main --stat` shows changes only in the two target files.
- [ ] **13. Cherry-pick isolation gate.** `git diff main -- app/services/ingestion.py` shows ONLY: counters, tolerate `except`, all-fail guard, metadata-None chain, summary log. NONE of: "first cardholder wins", `_metadata_completeness` deletion, `UNKNOWN-` placeholder removal, `_drop_empty_transactions` move, `fix/zen-max-tokens`. Commit: `fix(ingestion): tolerate partial chunk failures in chunked extraction`.
