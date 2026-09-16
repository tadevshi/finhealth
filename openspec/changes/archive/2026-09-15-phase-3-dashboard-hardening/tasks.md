# Tasks: Phase 3 Dashboard Hardening

## Review Workload Forecast

| Field | Value |
|-------|-------|
| Estimated changed lines | 1500–1750 (Phase 8 adds ~200–250 lines of evidence + ~20 lines of `seed_demo` collision guard + ~5 lines of comment + ~5–10 lines of canonical spec MODIFIED sync on top of Phases 1–7 baseline) |
| 400-line budget risk | High |
| Chained PRs recommended | No (user-approved `size:exception`, Phase 8 stays in the same PR) |
| Suggested split | Single PR covering Phases 1–5 (already merged/closed) + Phase 6 + Phase 7 remediation + Phase 8 evidence/cleanup |
| Delivery strategy | `exception-ok` (user pre-approved single PR) |
| Chain strategy | `size-exception` |
| Budget | ~200–250 (Phase 8 only) |

Decision needed before apply: No
Chained PRs recommended: No
Chain strategy: size-exception
400-line budget risk: Low

> Scope discipline: the 24 unrelated full-suite baseline failures (LLM Zen, ingestion, real-PDF/e2e, lifespan, CORS env leakage) remain explicitly OUT OF SCOPE for this change and are documented as repository baseline risk in `verify-report.md` (lines 149–158). No remediation tasks are added for them.

> Out-of-scope baseline failures: 16 LLM Zen schema/endpoint/header/retry, 3 ingestion, 3 real-PDF/e2e, 1 lifespan unreachable-path, 1 config CORS env-leakage. Documented in `verify-report.md`; tracked in a separate baseline issue, not here.

### Suggested Work Units

| Unit | Goal | Likely PR | Focused test command | Runtime harness | Rollback boundary |
|------|------|-----------|----------------------|-----------------|-------------------|
| WU-1 | Phases 6 + 7 remediation (seed provenance, API resolver, runtime tests, SQLite busy/integrity, service-contract regression, end-to-end seed, Docker-lifecycle disposable smoke) | PR 1 (size:exception) | `pytest tests/test_dashboard.py tests/test_dashboard_api.py tests/test_web_phase3.py tests/test_seed_demo.py tests/test_sqlite_ops.py tests/test_dashboard_selection.py tests/test_config.py tests/test_documentation.py tests/test_docker_lifecycle.py --no-cov` | `uvicorn` + `curl /dashboard`; `python -m app.cli.seed_demo` twice; `python -m app.cli.sqlite_ops backup`/`restore`; `docker compose -p finhealth-p7-<uuid8> -f <isolated-compose> up -d` (skipped if `docker compose` missing) | Revert changes in `app/services/dashboard.py`, `app/cli/seed_demo.py`, `app/api/v1/dashboard.py`, `app/cli/sqlite_ops.py`, `app/web/templates/partials/dashboard_summary.html`, plus new tests; app runs. New `tests/test_documentation.py` and `tests/test_docker_lifecycle.py` are additive — drop them to revert. |
| WU-2 | Phase 8 evidence + cleanup (full mapped seed columns, every user non-timestamp column, exact-ID/hash collision rejection, combined unknown-alias->seed->dashboard, isolated exact `docker compose down`/`up` + post-`down -v` + stopped-container backup + restored-DB HTTP smoke, stale comment cleanup, canonical pre-archive spec sync) | PR 1 (size:exception, same PR) | `pytest tests/test_seed_demo.py tests/test_dashboard.py tests/test_sqlite_ops.py tests/test_documentation.py tests/test_docker_lifecycle.py --no-cov` | `python -m app.cli.seed_demo` twice; `python -m app.cli.sqlite_ops backup`/`restore`; `docker compose -p finhealth-p8-<uuid8> -f <isolated-compose> down` then `up -d` then `curl /api/v1/dashboard/summary` then `down -v` against disposable `tmp_path` mounts (skipped if `docker compose` missing); `cp` stopped-container SQLite + WAL/SHM and run `PRAGMA integrity_check` against the copy | Revert new tests in `tests/test_seed_demo.py` and `tests/test_docker_lifecycle.py`; revert new helper in `app/cli/seed_demo.py` (e.g. `_assert_unmarked_collision`); revert stale docstring comment in `app/web/templates/partials/dashboard_summary.html`; revert sync of `openspec/specs/phase3-dashboard/spec.md` MODIFIED requirements from the existing delta. Archived `openspec/changes/archive/2026-07-13-phase-3-dashboard/` is NEVER touched. |

## Phase 1: Selection Resolver and Summary (WU-1)

- [x] 1.1 RED `tests/test_dashboard_selection.py`: `resolver("2026-07","ytd")` returns `(2026-01-01,2026-07-31)`; `all_time`+earliest `2025-02-10` returns `(2025-02-01,2026-07-31)`; API `range=0` maps to `all_time`.
- [x] 1.2 Create `app/services/dashboard_selection.py`: `DashboardSelection`, `RangeMode`, `resolve_window(selection,today,earliest)`, `from_api_range`, `labels`.
- [x] 1.3 Modify `app/services/dashboard.py`: divide `daily_avg_per_currency` by `calendar.monthrange(period)[1]`; aggregate `transaction_count_per_currency`; keep prev-month.
- [x] 1.4 Modify `app/schemas/dashboard.py`: add `transaction_count_per_currency: dict[str,int]`.
- [x] 1.5 Modify `app/api/v1/dashboard.py`: validate `range ∈ {0,3,6,12}`, map via `from_api_range`.

## Phase 2: Web, HTMX, Anomaly (WU-1)

- [x] 2.1 RED `tests/test_web_phase3.py`: `/dashboard` shows active cards only, dynamic labels, 5 sections, one HTMX form posting `period/card_id/range_mode`; no anomaly placeholder.
- [x] 2.2 Create `app/web/templates/partials/dashboard_sections.html` rendering all 5 sections.
- [x] 2.3 Modify `app/web/router.py`: add `GET /dashboard/sections`; one `DashboardSelection` per request.
- [x] 2.4 Modify `dashboard.html` + `dashboard_summary/categories/merchants/monthly/recurring.html`: dynamic labels, live counts, one HTMX form.
- [x] 2.5 Delete `app/web/templates/partials/dashboard_anomalies.html` and any references.

## Phase 3: Seed Aliases, Provenance, Upsert (WU-2)

- [x] 3.1 RED `tests/test_seed_demo.py`: alias normalization (case+Unicode, unknown->`Uncategorized`); two runs identical by stable key; user tx survives; no seed row on user statement.
- [x] 3.2 Add case-folded Unicode-normalized alias table; resolve plan keys to canonical names.
- [x] 3.3 Generate seed-provenance marker from UUIDv5 namespace + statement hash; mark only created rows.
- [x] 3.4 Reconcile by deterministic key only; (card+period) collisions skip; no table-wide deletes.
- [x] 3.5 `Uncategorized` row appears with `total_per_currency={}` and `count=0` when no seed spend.

## Phase 4: SQLite Path, Docker, Backup/Restore (WU-3)

- [x] 4.1 RED `tests/test_config.py` + `tests/test_sqlite_ops.py`: default URL `data/finhealth.db`; backup passes `integrity_check`; restore preserves counts; busy/corrupt/non-SQLite reject pre-mutation.
- [x] 4.2 Modify `app/core/config.py` + `.env.example` to canonical `data/finhealth.db`; drop prior `./finhealth.db`.
- [x] 4.3 Update `docker-compose.yml` + `docker-compose.self-hosted.yml`: bind-mount `./data` to `/app/data`; not a named volume.
- [x] 4.4 Create `app/cli/sqlite_ops.py`: `backup()` via `sqlite3` API + `integrity_check` + count manifest; `restore()` validates, rejects busy/non-SQLite, removes stale `-wal`/`-shm`, atomic-replace.
- [x] 4.5 Update `README.md`: accurate bind-mount, correct `down -v`, verified backup/restore runbook.

## Phase 5: Verification

- [x] 5.1 Run full `pytest`; every spec scenario from the three delta specs covered by name in test docstrings.

## Phase 6: Verification-Reported Remediation (scope: change-scoped findings only)

### 6.A Deterministic Seed Provenance and UUIDv5 Ownership (seed_demo)

- [x] 6.A.1 RED `tests/test_seed_demo.py::test_every_seed_row_carries_provenance_marker`: after one full `seed_demo()` run, every `Bank`, `CreditCard`, `Merchant`, `Category`, `Statement`, `Transaction`, and `RecurringRule` row MUST have `SEED_PROVENANCE` in its JSON/text marker column; user-inserted rows in the same DB stay unmarked.
- [x] 6.A.2 RED `tests/test_seed_demo.py::test_user_row_marker_unchanged_after_seed`: insert a pre-existing `Transaction` with `raw_json=None`; run `seed_demo()`; assert marker still `None` and every non-timestamp column unchanged (full value comparison, not just existence).
- [x] 6.A.3 GREEN Modify `app/cli/seed_demo.py`: add a small `_mark_seed_owned(row)` helper that sets the stable provenance marker for every newly created `Bank`/`CreditCard`/`Merchant`/`Category`/`Statement`/`Transaction`/`RecurringRule`; reuse `_seed_uuid(...)` so each `Bank.id`/`CreditCard.id`/`Merchant.id`/`Category.id`/`RecurringRule.id` is deterministic from a stable key (idempotent reruns return the same row).
- [x] 6.A.4 GREEN Extend `_get_or_create_*` paths in `app/cli/seed_demo.py` to assign UUIDv5 IDs from a stable per-entity key (`bank/{name}`, `card/{mask}`, `merchant/{slug}`, `category/{name}`, `recurring/{merchant_id}/{currency}/{period_days}`) so a second run finds the same row by ID and never falls back to default UUIDs.
- [x] 6.A.5 REFACTOR Remove dead fallback paths in `app/cli/seed_demo.py` that create rows without setting a marker; the marker MUST be set in the same `add()` call site.

### 6.B Dashboard `range=0` Resolver Wiring and Historical Coverage

- [x] 6.B.1 RED `tests/test_dashboard_api.py::test_summary_range_zero_reaches_resolver_with_all_time`: call `GET /api/v1/dashboard/summary?period=2026-07&range=0&card_id=all`; assert service received `range_mode="all_time"` (capture via monkeypatched `from_api_range` return, or assert response `daily_avg` matches calendar days, prior-month comparison, and at least one transaction from a historical month).
- [x] 6.B.2 RED `tests/test_dashboard.py::TestSummary::test_summary_range_zero_returns_all_time_with_history`: seed transactions in `2025-03`, `2026-01`, `2026-07`; `summary("2026-07", 0, "all")`; assert `transaction_count` is the full count, `daily_avg` uses `/31`, `comparison_to_prev_period_pct_per_currency` compares against `2026-06` only.
- [x] 6.B.3 GREEN Modify `app/api/v1/dashboard.py`: replace the `from_api_range(range_months)` call inside `_parse_range` so it returns a `RangeMode` that is then forwarded into `DashboardService.compose_summary(...)` (or `summary(...)`) as a `range_mode` argument; the endpoint MUST NOT discard the translated mode.
- [x] 6.B.4 GREEN Modify `app/services/dashboard.py::DashboardService.summary` to accept a `range_mode: RangeMode | None` parameter; when `range_mode is not None`, restrict the aggregation window to the resolver window (excluding the comparison month) while keeping `daily_avg` denominated on the calendar month; when `None`, keep current behavior for backward compatibility.
- [x] 6.B.5 REFACTOR Remove the `del range_months` line in `app/services/dashboard.py` once `range_mode` is plumbed; update docstring to describe the windowed aggregation.

### 6.C Runtime Tests for Missing Dashboard Scenarios

- [x] 6.C.1 RED `tests/test_web_phase3.py::test_dashboard_card_picker_excludes_inactive_cards`: seed 2 active + 1 inactive `CreditCard`; `GET /dashboard`; assert the inactive card's `card_number_masked` is NOT present in the picker `<select>` options but both active masks ARE.
- [x] 6.C.2 RED `tests/test_web_phase3.py::test_dashboard_summary_renders_usd_count_from_payload`: seed 2 USD + 5 CLP transactions; `GET /dashboard/sections?period=2026-07&...`; parse the partial HTML and assert the rendered USD count `2` is present next to the USD sub-block, not hard-coded.
- [x] 6.C.3 RED `tests/test_seed_demo.py::test_seed_collision_user_statement_same_card_period`: insert a pre-existing `Statement` on a seed card for `(2026, 7)` (user-owned, no `file_hash` collision) plus a user transaction on it; run `seed_demo()`; assert the user statement is untouched, a separate seed statement exists with the deterministic hash, and no seed transaction is attached to the user statement.
- [x] 6.C.4 RED `tests/test_dashboard.py::TestCategories::test_uncategorized_with_seed_spend`: insert 2 seed transactions resolving to `Uncategorized` (alias `"something_new"`); call `DashboardService.categories`; assert the `Uncategorized` row's `total_per_currency` reflects those 2 transactions and `transaction_count == 2`.
- [x] 6.C.5 RED `tests/test_seed_demo.py::test_two_runs_user_value_equality`: insert a user `Transaction` with non-null `raw_json={"note":"keep"}`; run `seed_demo()` twice; after the second run, assert the user transaction's `raw_json`, `amount`, `currency`, `date`, and `description` are byte-identical to the original.
- [x] 6.C.6 RED `tests/test_dashboard.py::TestSummary::test_summary_empty_period_returns_zero_counts_and_empty_per_currency`: zero-transaction period; assert `total_per_currency == {}`, `transaction_count == 0`, `transaction_count_per_currency == {}`, `top_*` fields are `None`, `period_start`/`period_end` are still populated.
- [x] 6.C.7 GREEN Wire all the new tests through the same fixtures used by the existing `test_dashboard.py` / `test_web_phase3.py` / `test_seed_demo.py`; no production code change expected — these are pure coverage additions.

### 6.D SQLite Restore Busy-Destination Protection and Post-Restore Verification

- [x] 6.D.1 RED `tests/test_sqlite_ops.py::test_restore_rejects_busy_destination_before_mutation`: open a long-lived `sqlite3.connect(destination_url)` writer; call `restore(...)`; assert `RuntimeError`/`ValueError` raised, the destination file is unchanged (byte-equal to its pre-call state), and the stale `-wal`/`-shm` files are still present.
- [x] 6.D.2 RED `tests/test_sqlite_ops.py::test_restore_post_verification_counts_and_integrity`: restore a known backup; assert the post-restore destination file (a) passes `PRAGMA integrity_check` and (b) `_counts(destination)` returns the same dict as the `BackupManifest.counts` from the original backup.
- [x] 6.D.3 GREEN Modify `app/cli/sqlite_ops.py`: in `restore(...)`, before any mutation, attempt `sqlite3.connect(destination)` with a short timeout and a `BEGIN IMMEDIATE`/`PRAGMA quick_check`; if the connection fails or the destination is locked/writer-present, raise `RuntimeError("destination busy; stop the application before restore")` and exit without touching the destination.
- [x] 6.D.4 GREEN Modify `app/cli/sqlite_ops.py`: after atomic replacement in `restore(...)`, reopen the destination and (a) run `PRAGMA integrity_check`; (b) compare `_counts(destination)` against the recorded `BackupManifest.counts`; raise if either check fails. Return the verified manifest to the CLI entry point.
- [x] 6.D.5 GREEN Extend `app/cli/sqlite_ops.py` `main()` so `restore` prints a `post_verification` block (integrity, counts delta vs manifest) when verification passes; do NOT add a network/HTTP endpoint — the operator runs the documented `curl /health` / `curl /dashboard` smoke checks after restart, as scoped in the design.
- [x] 6.D.6 RED `tests/test_sqlite_ops.py::test_restore_rejects_non_sqlite_url`: `restore(backup_path, "postgresql://...")`; assert `ValueError` before any filesystem mutation.
- [x] 6.D.7 REFACTOR Extract the busy-destination probe into a private `_assert_destination_idle(path)` helper in `app/cli/sqlite_ops.py` and reuse it from both `restore` and any future path-mutating helper; keep the helper limited to the SQLite file boundary — do NOT shell out to `lsof`/`fuser`.

### 6.E Stale `distinct-day` Helper and Docstring Update

- [x] 6.E.1 RED `tests/test_dashboard.py::test_distinct_days_helper_docstring_or_removal`: assert either (a) the helper is removed and no other code references it, OR (b) its docstring explicitly states the new semantics (calendar days of the month, NOT distinct transaction days).
- [x] 6.E.2 GREEN Modify `app/services/dashboard.py`: either delete `_distinct_days_per_currency` (and any internal callers) or rewrite the docstring to describe the calendar-month denominator and the fact that it is no longer used by `summary`; remove the obsolete "divides by the distinct days" wording.
- [x] 6.E.3 REFACTOR `ruff check app tests` MUST pass after the helper change; ensure no template or comment still references the old distinct-day semantics.

### 6.F Remediation Verification

- [x] 6.F.1 Run focused suite: `pytest tests/test_dashboard.py tests/test_dashboard_api.py tests/test_web_phase3.py tests/test_seed_demo.py tests/test_sqlite_ops.py tests/test_dashboard_selection.py --no-cov`; assert every previously UNTESTED/PARTIAL/FAILING scenario in `verify-report.md` is now green.
- [x] 6.F.2 Run `ruff check app tests` and `python -m compileall -q app tests`; both MUST exit 0.
- [x] 6.F.3 Document in `verify-report.md` re-run summary: number of UNTESTED/PARTIAL/FAILING scenarios that became COMPLIANT; leave the 24 unrelated full-suite baseline failures explicitly out of scope.

## Phase 7: Verify-Report-Reported Gaps (scope: 3 remaining change-scoped findings)

> Pre-approved delivery: `exception-ok` (single PR, `size:exception`). No new decision needed before apply.
> 24 unrelated full-suite baseline failures remain explicitly OUT OF SCOPE.

### 7.A `DashboardService.summary(period, range=0, card_id=...)` regression coverage (WU-1)

Depends on: Phase 6.B (resolver + `range_mode` already wired).

- [x] 7.A.1 RED `tests/test_dashboard.py::TestSummary::test_summary_two_arg_infers_all_time_when_range_mode_omitted`: call `service.summary(date(2026,7,1), 0, "all")` with only positional `period/range_months/card_id` (no `range_mode`); assert the service infers `RangeMode.all_time()` so `transaction_count` includes 2025-03 + 2026-01 + 2026-07 rows, `daily_avg_per_currency` uses `/31`, and `comparison_to_prev_period_pct_per_currency` still compares against `2026-06` only.
- [x] 7.A.2 RED `tests/test_dashboard.py::TestSummary::test_summary_explicit_range_mode_overrides_inference`: pass `range_mode=RangeMode.rolling(3)` alongside `range_months=0`; assert the explicit mode wins and the window is the last 3 months, not all-time (lock the precedence contract).
- [x] 7.A.3 RED `tests/test_dashboard_api.py::TestDashboardSummary::test_summary_range_zero_service_receives_all_time_inference`: monkeypatch `app.services.dashboard.DashboardService.summary` to capture the kwargs; `GET /api/v1/dashboard/summary?period=2026-07&range=0&card_id=all`; assert the captured call used `RangeMode.all_time()` and `range_months=0` together (covers the API->service wire path).
- [x] 7.A.4 GREEN Modify `app/services/dashboard.py::DashboardService.summary`: when `range_mode is None` AND `range_months == 0`, treat the call as all-time by synthesizing `RangeMode.all_time()` internally; keep `range_months` semantics for `3/6/12` (rolling lookback default) and reject any other integer with `ValueError`. Document the inference in the docstring.
- [x] 7.A.5 GREEN Modify `app/api/v1/dashboard.py::dashboard_summary`: keep forwarding `range_mode=parsed_range` (already in place from 6.B.3); no behaviour change, but add a docstring note that the service also infers all-time when called directly with `range_months=0`.
- [x] 7.A.6 REFACTOR Re-run focused suite; ensure the explicit-`range_mode` test (7.A.2) and the inference test (7.A.1) coexist without overlap; remove any temporary logging added during 7.A.4.

### 7.B End-to-end seed verification with all seed-owned row types (WU-1)

Depends on: Phase 6.A (provenance + UUIDv5 already in place) + Phase 6.C.4 (`Uncategorized` spend test already passing in isolation).

- [x] 7.B.1 RED `tests/test_seed_demo.py::test_two_runs_seed_owned_row_snapshot_stable`: run `seed_demo()` twice against a fresh `tmp_path` DB; assert byte-identical snapshots of every seed-owned row type (Bank, CreditCard, Merchant, Category, Statement, Transaction, RecurringRule) — same `id`, same marker, same non-timestamp columns. Use the existing `_seed_uuid` namespace so the snapshot is deterministic.
- [x] 7.B.2 RED `tests/test_dashboard.py::TestCategories::test_uncategorized_end_to_end_through_seed`: after one `seed_demo()` run, call `DashboardService.categories(period=date(2026,7,1))`; assert the `Uncategorized` row's `total_per_currency` and `transaction_count` are both `{}` / `0` (no seed plan row currently resolves to `Uncategorized`). Then insert a 2-row user fixture whose `category_id` is the `Uncategorized` UUID and assert the dashboard picks those up — this proves the alias-fallback end-to-end path through the seed table into the dashboard.
- [x] 7.B.3 RED `tests/test_seed_demo.py::test_unknown_alias_routes_to_uncategorized_via_seed`: extend `TX_PLAN` with a sentinel `("2026-7", "completely_new_alias_xyz", 1, "CLP")` row in a test-local plan; run a test-scoped `seed_demo` loop that registers it; assert the new transaction's `category` string is `"Uncategorized"` and the seeded Category row with that name exists with the marker.
- [x] 7.B.4 GREEN Confirm 7.B.1 / 7.B.2 / 7.B.3 pass without any production-code change (they are pure coverage additions on top of 6.A + 6.C.4). Document in the test docstring that user rows remain untouched across all three scenarios.
- [x] 7.B.5 REFACTOR Snapshot helper in `tests/test_seed_demo.py` returns a typed dict keyed by row type; remove any per-test ad-hoc SQL.

### 7.C SQLite runtime + documentation evidence (disposable, no developer state mutation) (WU-1)

Depends on: Phase 4 (config + compose + CLI already in place).

- [x] 7.C.1 RED `tests/test_config.py::test_default_database_url_is_canonical_data_path`: assert `Settings().DATABASE_URL == "sqlite+aiosqlite:///data/finhealth.db"` (matches the bind-mount path inside the container, so host and container agree).
- [x] 7.C.2 RED `tests/test_sqlite_ops.py::test_restore_post_verification_endpoint_smoke`: after a successful `restore(...)`, call `app.cli.sqlite_ops._counts(destination)` and assert it equals `manifest.counts`; then open the just-restored DB via SQLAlchemy, run a `SELECT COUNT(*)` on `transactions/statements/credit_cards/banks`, and assert the counts match — proves the restored DB is the same shape the app reads.
- [x] 7.C.3 RED `tests/test_documentation.py::test_readme_docker_runbook_claims` (new file, ≤ 80 lines): assert `README.md` contains (a) the exact `python -m app.cli.sqlite_ops backup ...` and `restore ...` invocations from the runbook, (b) the `down -v` clarification ("does NOT delete bind-mounted host directories"), (c) the bind-mount table (`./shared`, `./data`), and (d) the post-restore smoke-check `curl /api/v1/health` line. Fail with a clear message naming the missing line.
- [x] 7.C.4 RED `tests/test_documentation.py::test_compose_bind_mount_uses_data_directory`: parse `docker-compose.yml` and `docker-compose.self-hosted.yml` with `yaml.safe_load`; assert the `finhealth` service mounts `./data:/app/data` (host bind, not a named volume) and the `DATABASE_URL` env var is `sqlite+aiosqlite:////app/data/finhealth.db` in both files.
- [x] 7.C.5 RED `tests/test_docker_lifecycle.py::test_disposable_compose_lifecycle_persists_data` (new file, ≤ 120 lines): use a **temporary compose project name** (`COMPOSE_PROJECT_NAME=finhealth-p7-<uuid8>`) and **isolated bind-mount dirs** under `tmp_path` (a copied `docker-compose.yml` with `./data` and `./shared` redirected to `tmp_path`); assert (a) `up -d` succeeds, (b) a row written through the running container survives `stop` + `start` (data persists), and (c) `down` (no `-v`) does NOT remove the host data dir. **Skip the test (mark xfail with a clear reason) if `docker compose` is unavailable** — do NOT fail the suite on developer machines without Docker, and never touch the developer's default Compose project.
- [x] 7.C.6 GREEN Add a one-time guard helper `_require_docker()` in `tests/test_docker_lifecycle.py` that returns `False` when `docker compose version` exits non-zero; the lifecycle test is then conditionally executed and reports `xfail` (not error) when Docker is missing. No production code change.
- [x] 7.C.7 REFACTOR Add a `make verify` (or extend `scripts/verify.sh`) target that runs: (1) the focused pytest suite from 6.F.1, (2) `ruff check app tests`, (3) `python -m compileall -q app tests`, (4) `pytest tests/test_documentation.py tests/test_docker_lifecycle.py` (last one is xfail without Docker). Document the target in the runbook section of `README.md`.

### 7.D Phase 7 Verification

- [x] 7.D.1 Run `pytest tests/test_dashboard.py tests/test_dashboard_api.py tests/test_web_phase3.py tests/test_seed_demo.py tests/test_sqlite_ops.py tests/test_dashboard_selection.py tests/test_config.py tests/test_documentation.py tests/test_docker_lifecycle.py --no-cov`; assert every newly added scenario is green (Docker-lifecycle may xfail when Docker is unavailable — that is a PASS for that environment, not a regression).
- [x] 7.D.2 Run `ruff check app tests` and `python -m compileall -q app tests`; both MUST exit 0.
- [x] 7.D.3 Re-run the full `pytest` suite; the 24 unrelated baseline failures MUST still be red (no regression in scope), the Phase 7 scenarios MUST be green or xfail.

## Phase 8: Verify-Report Evidence/Behavior Gaps (scope: 3 remaining CRITICAL findings + 1 WARNING)

> Pre-approved delivery: `exception-ok` (single PR, `size:exception`). Phase 8 stays in the same PR as Phases 6 + 7. No new decision needed before apply.
> 24 unrelated full-suite baseline failures remain explicitly OUT OF SCOPE.
> Archived history `openspec/changes/archive/2026-07-13-phase-3-dashboard/` is NEVER altered; only the live canonical `openspec/specs/phase3-dashboard/spec.md` may be synced from the existing MODIFIED delta.
> All Docker/Compose work below uses unique `finhealth-p8-<uuid8>` project names and disposable `tmp_path` bind mounts; the developer's default Compose project and the repository `./data` directory are NEVER touched.

### 8.A Seed Ownership Evidence Completion (WU-2)

Depends on: Phase 6.A (provenance + UUIDv5 already in place) + Phase 7.B (typed snapshot helper already present).

- [x] 8.A.1 RED `tests/test_seed_demo.py::test_two_runs_full_mapped_column_snapshot`: extend `_seed_owned_snapshot` to include every mapped non-timestamp column per row type — `Bank` adds `password_formula` (already there) and confirms no missing `created_at`/`updated_at` snapshot divergence; `CreditCard` adds `currency`; `Merchant` adds `default_category_id` resolved to the canonical name; `Category` adds `sort_order`; `Statement` adds `period_start`/`period_end`/`statement_date`/`file_path`/`file_hash`/`status`; `Transaction` MUST add `installment_number`, `installment_total`, `installment_value`, `recurring_rule_id`, `low_confidence`; `RecurringRule` adds `amount_min`, `amount_max`, `confidence`, `occurrences`, `last_seen_date`, `is_active`. Assert byte-identical snapshots across two `seed_demo()` runs.
- [x] 8.A.2 RED `tests/test_seed_demo.py::test_two_runs_user_full_value_equality`: insert a user `Transaction` with `raw_json={"note":"keep"}`, `installment_number=2`, `installment_total=12`, `installment_value=Decimal("5000.00")`, `recurring_rule_id=<seed_rule_id>`, `low_confidence=True`, `merchant_id=<seed_merchant_id>`, `category_id=<seed_category_id>`, `category="Dining Out"`; run `seed_demo()` twice; after the second run, assert every non-timestamp mapped column (`raw_json`, `amount`, `currency`, `date`, `description`, `installment_number`, `installment_total`, `installment_value`, `recurring_rule_id`, `low_confidence`, `merchant_id`, `category_id`, `category`) is byte-identical to the original. Uses SQLAlchemy `inspect(Transaction).columns` as the mapped-column oracle so a new column auto-fails this test until reviewed.
- [x] 8.A.3 RED `tests/test_seed_demo.py::test_exact_id_collision_rejected_for_every_seed_entity`: for each seed-owned entity (`Bank`, `CreditCard`, `Merchant`, `Category`, `RecurringRule`), pre-insert an unmarked row with the deterministic UUID from `_seed_uuid(stable_key)` plus a non-seed `display_name`/`name`/`cardholder`/`period_label`; call `seed_demo()`; assert the pre-existing row is untouched AND no second row is created (the existing `Statement`/`Transaction` collision path is reused as the reference, the other 5 entities are the new coverage).
- [x] 8.A.4 RED `tests/test_seed_demo.py::test_exact_file_hash_collision_rejected_for_statement`: pre-insert an unmarked `Statement` with `file_hash = _statement_hash(str(card.id), 2026, 7)` and a user-only `error_message`; call `seed_demo()`; assert the user statement is untouched AND no second seed statement with the same hash is created (locks the `Statement.file_hash` UNIQUE constraint path).
- [x] 8.A.5 RED `tests/test_seed_demo.py::test_unknown_alias_seeds_two_uncategorized_dashboard_rows`: extend `TX_PLAN` with two sentinel rows whose category_key is unknown (`"completely_new_alias_a"` CLP 100, `"completely_new_alias_b"` CLP 200); run `seed_demo()`; assert both transactions carry `category == "Uncategorized"`, the seeded `Category` row with `name == "Uncategorized"` exists with the marker, AND `DashboardService.categories(period=date(2026,7,1))` reports the `Uncategorized` row with `total_per_currency == {"CLP": Decimal("300.00")}` and `transaction_count == 2`. This is the combined unknown-alias -> seed -> dashboard `Uncategorized` test.
- [x] 8.A.6 GREEN Modify `app/cli/seed_demo.py`: add a private helper `_assert_unmarked_or_seed(row, stable_key)` that raises `RuntimeError` when an existing row at the deterministic ID/hash is found but its stored marker (or marker-bearing text/JSON column) is NOT the seed marker. Wire it into the `_get_or_create_*` paths for `Bank`/`CreditCard`/`Merchant`/`Category`/`RecurringRule` and into the statement/transaction collision paths so every seed-owned entity rejects an unmarked exact-ID/hash collision BEFORE any mutation. The pre-existing Bank natural-name fallback is removed — the deterministic UUID is the only lookup key.
- [x] 8.A.7 REFACTOR `_seed_owned_snapshot` returns a typed `SeedOwnedSnapshot` dataclass so future column additions fail the dataclass at import; the `inspect(Transaction).columns` oracle in 8.A.2 lives in a single shared helper `tests/test_seed_demo.py::_mapped_columns(model)`.

### 8.B SQLite/Docker Evidence Completion (WU-2)

Depends on: Phase 4 (config + compose + CLI already in place) + Phase 7.C (disposable Docker harness already exists).

- [x] 8.B.1 RED `tests/test_docker_lifecycle.py::test_disposable_compose_down_up_persists_data` (new test in the same file): extend the existing harness with a copy of the compose file at `tmp_path / "compose.yml"` that uses the `python:3.12-slim` image; use `COMPOSE_PROJECT_NAME=finhealth-p8-<uuid8>`; run `docker compose -p <project> -f <compose> up -d`; assert host data file exists; then run `docker compose -p <project> -f <compose> down`; then `docker compose -p <project> -f <compose> up -d` again; assert the host bind-mounted `data/finhealth.db` still contains the original row (the exact `down` + `up` lifecycle, no `stop` shortcut). All bind mounts are under `tmp_path`; the developer's default Compose project and `./data` directory are NEVER touched. Mark `xfail` when `docker compose` is unavailable.
- [x] 8.B.2 RED `tests/test_docker_lifecycle.py::test_disposable_compose_down_dash_v_preserves_host_data` (new test in the same file): after the lifecycle in 8.B.1, run `docker compose -p <project> -f <compose> down -v`; assert the host `tmp_path / "data"` directory AND the `finhealth.db` file are still present and byte-equal to their pre-call state (proves the `-v` flag does NOT remove bind-mounted host data). Cleanup `finally` block uses a separate disposable project; the developer's default Compose project and `./data` directory are NEVER touched. Mark `xfail` when `docker compose` is unavailable.
- [x] 8.B.3 RED `tests/test_sqlite_ops.py::test_stopped_container_backup_copy_passes_integrity` (new test): assert the documented stopped-container copy procedure works end-to-end — create a fresh `tmp_path / "data/finhealth.db"` with a `lifecycle` table; write a row; run a function `copy_stopped_container_db(data_dir, dest)` that performs a plain filesystem `shutil.copy2` of `finhealth.db` (and `-wal`/`-shm` if present) into a disposable destination; assert the destination passes `PRAGMA integrity_check` against a copy opened in a fresh `sqlite3.connect`. Test is pure-Python; no Docker required. Helper `copy_stopped_container_db` lives in `app/cli/sqlite_ops.py` so the README runbook and the test share one implementation.
- [x] 8.B.4 RED `tests/test_docker_lifecycle.py::test_restored_database_serves_dashboard_http_200` (new test, ≤ 80 lines): use a disposable isolated `tmp_path` data dir + a temporary FinHealth container launched with the real `docker-compose.yml` (mounting `tmp_path / "data"` as the bind-mount), an isolated `COMPOSE_PROJECT_NAME=finhealth-p8-<uuid8>`, and `DATABASE_URL=sqlite+aiosqlite:////app/data/finhealth.db`; seed once; `curl http://localhost:<mapped>/api/v1/dashboard/summary?period=2026-07&range=6&card_id=all` returns HTTP 200; then run the `restore` against the bind-mounted DB (via the disposable app); restart the container; `curl` again returns HTTP 200 and the JSON contains the seeded transaction counts. The test uses a disposable `tmp_path` mount; the developer's default Compose project and `./data` directory are NEVER touched. Mark `xfail` when `docker compose` is unavailable. The test MUST be in `tests/test_docker_lifecycle.py` (not a new file) so the existing `_require_docker` guard is reused.
- [x] 8.B.5 GREEN Modify `app/cli/sqlite_ops.py`: export `copy_stopped_container_db(source_dir, dest_path)` that copies `finhealth.db` (and `-wal`/`-shm` if present) via `shutil.copy2`, returns the destination `Path`, and is documented in the module docstring as the implementation backing the README "stopped container" backup variant. No production behaviour change.
- [x] 8.B.6 GREEN Update `README.md` runbook: add a one-line entry for the stopped-container backup variant that points at `python -c "from app.cli.sqlite_ops import copy_stopped_container_db; copy_stopped_container_db('data', 'backups/finhealth-stopped.db')"` (or equivalent documented invocation); add a one-line entry for the exact `docker compose down` + `docker compose up` lifecycle; add a one-line entry for the post-`down -v` host-data preservation note. Keep the existing backup API and runbook entries intact.
- [x] 8.B.7 RED `tests/test_documentation.py::test_readme_documents_stopped_container_backup` (new test, ≤ 30 lines): assert `README.md` contains the exact `copy_stopped_container_db` invocation from 8.B.6 AND the exact `down -v` does-NOT-remove-host-data wording AND the exact `docker compose down` + `up` lifecycle line. Fail with a clear message naming the missing line.
- [x] 8.B.8 REFACTOR Extract the `docker compose` invocations in `tests/test_docker_lifecycle.py` into a small helper `tests/test_docker_lifecycle.py::_compose_exec(project, compose_file, *args)` so 8.B.1/8.B.2/8.B.4 share one call site; `_require_docker` is reused unchanged. No production code change.

### 8.C Stale Comment Cleanup and Canonical Spec Sync (WU-2)

Depends on: Phase 6.E (distinct-day helper already removed) + Phase 7.A (resolver + `range_mode` already plumbed).

- [x] 8.C.1 RED `tests/test_dashboard.py::test_dashboard_summary_partial_no_hard_coded_recurring_claim`: render `partials/dashboard_summary.html` via the existing `dashboard_section_summary` route with `recur_count=0`; assert the rendered HTML does NOT contain the literal string "hard-coded to 8" and does NOT contain the literal "v5 design" (or "real value will come from the recurring service in a later phase") — the live values from `recur_count`/`recur_monthly` are the only signal. The test pins the docstring + the v5-era narrative as removed.
- [x] 8.C.2 GREEN Modify `app/web/templates/partials/dashboard_summary.html`: rewrite the top-of-file docstring (lines 1–19) to describe the live `recur_count`/`recur_monthly` contract and remove the "hard-coded to 8 from the v5 design" sentence; rewrite the Card 4 docstring (lines 151–154) to drop the "When the count is 0 the card shows a single em-dash so the user is never misled" copy into a single factual line. Behaviour is unchanged; the change is documentation only.
- [x] 8.C.3 GREEN Update `openspec/specs/phase3-dashboard/spec.md`: apply the `MODIFIED` block for the Summary KPI requirement that already lives in `openspec/changes/phase-3-dashboard-hardening/specs/phase3-dashboard/spec.md` — replace the three superseded "distinct days" lines (the `daily_avg_per_currency` wording on line 11, the `<distinct days>` expectation on line 18, and the "period's distinct days, not the full history" wording on line 40) with the calendar-month-denominator wording the delta already specifies. This is the standard SDD archive-path sync the OpenSpec convention permits.
- [x] 8.C.4 RED `tests/test_documentation.py::test_canonical_phase3_spec_uses_calendar_day_denominator` (new test, ≤ 30 lines): assert `openspec/specs/phase3-dashboard/spec.md` no longer contains the literal string "distinct days" AND contains the literal string "calendar days of the period month" (the new canonical wording). Asserts the sync is in place.
- [x] 8.C.5 GUARD Do NOT modify `openspec/changes/archive/2026-07-13-phase-3-dashboard/` under any circumstance — that folder is the audit trail and the OpenSpec convention forbids touching it. The 8.C.3 sync targets only the live canonical spec.
- [x] 8.C.6 REFACTOR Run `ruff check app tests`; ensure no new lint errors; the comment-only change in 8.C.2 MUST keep the file parseable by Jinja (no unclosed `{# ... #}` blocks).

### 8.D Phase 8 Verification

- [x] 8.D.1 Run `pytest tests/test_seed_demo.py tests/test_dashboard.py tests/test_sqlite_ops.py tests/test_docker_lifecycle.py tests/test_documentation.py tests/test_dashboard_api.py tests/test_web_phase3.py tests/test_dashboard_selection.py tests/test_config.py --no-cov`; assert every Phase 8 scenario is green. Docker-lifecycle scenarios (8.B.1, 8.B.2, 8.B.4) may xfail when `docker compose` is unavailable — that is a PASS for that environment, not a regression.
- [x] 8.D.2 Run `ruff check app tests` and `python -m compileall -q app tests`; both MUST exit 0.
- [x] 8.D.3 Re-run the full `pytest` suite; the 24 unrelated baseline failures MUST still be red (no regression in scope), the Phase 8 scenarios MUST be green or xfail.
- [x] 8.D.4 Update `openspec/changes/phase-3-dashboard-hardening/verify-report.md` re-run summary: number of UNTESTED/PARTIAL scenarios that became COMPLIANT; leave the 24 unrelated full-suite baseline failures explicitly out of scope; the CRITICAL findings from the prior report (1) seed ownership proof, (2) `Uncategorized` end-to-end, (3) SQLite lifecycle/restore) MUST each move to COMPLIANT or to a new `WARNING` with documented rationale.
