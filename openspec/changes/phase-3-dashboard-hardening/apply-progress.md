# Apply Progress: Phase 3 Dashboard Hardening

## Delivery

- Mode: `size:exception` — maintainer explicitly approved one PR despite review-budget risk.
- Artifact store: hybrid (`openspec` + Engram).
- Implementation mode: Standard; strict TDD is not enabled for this project.
- Current batch: Phase 8 evidence/behavior completion only. Completed Phase 1–7 work was not repeated, and recurring-detection implementation was not touched.

## Completed Tasks

- [x] 1.1–1.5 — Added the dashboard selection/range resolver, calendar-day averages, per-currency counts, and API `range=0` compatibility baseline.
- [x] 2.1–2.5 — Added unified HTMX section rendering, dynamic labels, active-card selection, live counts, and removed the unavailable anomaly placeholder.
- [x] 3.1–3.5 — Added normalized category aliases, UUIDv5 seed provenance baseline, idempotent seed reconciliation, user-row protection, and `Uncategorized` handling.
- [x] 4.1–4.5 — Added canonical SQLite path handling, backup/restore operations, integrity/count manifests, Docker persistence documentation, and runbook updates.
- [x] 5.1 — Ran the focused phase suite and recorded unrelated full-suite failures.
- [x] 6.A.1–6.A.5 — Added runtime seed provenance tests and deterministic UUIDv5 ownership for seed-created `Bank`, `CreditCard`, `Merchant`, `Category`, `Statement`, `Transaction`, and `RecurringRule` rows while preserving user-owned rows.
- [x] 6.B.1–6.B.5 — Plumbed API `range=0` through `RangeMode.all_time` into `DashboardService.summary(...)` and resolver-backed aggregation with historical coverage.
- [x] 6.C.1–6.C.7 — Added runtime tests for inactive-card exclusion, rendered USD count, same-card/same-period seed collision, Uncategorized seed spend, full user-row value preservation, empty-period shape, and dashboard wiring.
- [x] 6.D.1–6.D.7 — Added SQLite restore busy-destination guard, non-SQLite restore rejection, post-restore integrity/count verification, and CLI `post_verification` output.
- [x] 6.E.1–6.E.3 — Removed stale distinct-day helper semantics; `summary` now documents resolver-window aggregation and calendar-month denominators.
- [x] 6.F.1–6.F.3 — Re-ran focused suites, recurring regressions, Ruff, compileall, runtime seed/SQLite/web harnesses, and updated `verify-report.md` with the remediation summary.
- [x] 7.A.1–7.A.6 — Added direct `DashboardService.summary(..., range_months=0)` all-time inference, explicit `range_mode` precedence coverage, API forwarding regression coverage, and focused verification.
- [x] 7.B.1–7.B.5 — Added all-table seed-owned snapshot stability coverage, end-to-end `Uncategorized` dashboard evidence, unknown-alias-through-seed coverage, and a typed seed snapshot helper.
- [x] 7.C.1–7.C.7 — Added canonical database URL evidence, SQLAlchemy restore smoke coverage, README/Compose assertions, isolated disposable Docker lifecycle coverage, and `scripts/verify.sh` runbook documentation.
- [x] 7.D.1–7.D.3 — Re-ran the focused phase suite, recurring regressions, Ruff, compileall, isolated wheel build, Compose validation, disposable Docker harness, runtime seed/backup/restore/web smokes, and full-suite baseline check.
- [x] 8.A.1–8.A.7 — Completed seed ownership evidence: full mapped seed snapshots, full user non-timestamp value equality, exact-ID/hash collision rejection before mutation, and combined unknown-alias → seed → dashboard `Uncategorized` coverage.
- [x] 8.B.1–8.B.8 — Completed SQLite/Docker evidence: exact isolated `docker compose down`/`up`, `down -v` host-bind preservation, stopped-container backup copy helper, restored DB dashboard summary HTTP smoke, README runbook entries, and shared Compose helper extraction.
- [x] 8.C.1–8.C.6 — Removed stale hard-coded recurring narrative and synced only the live canonical Phase 3 spec from the existing MODIFIED delta; archived history remained untouched.
- [x] 8.D.1–8.D.4 — Ran Phase 8 focused verification, Docker verification, Ruff, compileall, runtime seed/backup/restore/copy harness, full-suite baseline, and updated `verify-report.md`.

## Work Unit Evidence

| Work Unit | Focused test command and exact result | Runtime harness command/scenario and exact result | Rollback boundary |
|---|---|---|---|
| WU-1 Selection/API/Web (Phases 1–2 baseline) | `pytest tests/test_dashboard_selection.py tests/test_dashboard.py tests/test_dashboard_api.py tests/test_web_phase3.py tests/test_seed_demo.py tests/test_config.py tests/test_sqlite_ops.py --no-cov` → 77 passed before Phase 6 | ASGITransport `/dashboard`, `/dashboard/sections`, and dashboard summary API returned 200 before Phase 6 | Revert selection service, dashboard service/schema/API/router/templates, and dashboard tests |
| WU-2 Demo seeding (Phase 3 baseline) | `tests/test_seed_demo.py` baseline included in 77 passing tests before Phase 6 | `python -m app.cli.seed_demo` twice produced no duplicate seed transactions/rules before Phase 6 | Revert `app/cli/seed_demo.py` and seed tests |
| WU-3 SQLite ops/config/docs (Phase 4 baseline) | SQLite operation tests included in 77 passing tests before Phase 6 | Backup/restore reported integrity `ok` and preserved counts before Phase 6 | Revert config/engine/sqlite CLI/docs/compose and related tests |
| WU-4 Phase 6 remediation | RED evidence: selected Phase 6 tests initially failed (`test_every_seed_row_carries_provenance_marker`, API/service all-time range tests, busy restore guard, distinct-day helper test). GREEN evidence: `pytest tests/test_dashboard.py tests/test_dashboard_api.py tests/test_web_phase3.py tests/test_seed_demo.py tests/test_sqlite_ops.py tests/test_dashboard_selection.py --no-cov` → 85 passed in 4.55s | `seed_demo` twice succeeded; `sqlite_ops backup` produced integrity `ok`; `sqlite_ops restore` printed `post_verification.integrity=ok` and zero count deltas; `uvicorn app.main:create_app --factory` served `/dashboard` with HTTP 200 | Revert Phase 6 changes in `app/cli/seed_demo.py`, `app/api/v1/dashboard.py`, `app/services/dashboard.py`, `app/web/router.py`, `app/cli/sqlite_ops.py`, and Phase 6 tests; keep unrelated Phase 1–5 baseline intact |
| WU-5 Phase 7 final remediation | RED evidence: `pytest tests/test_dashboard.py::TestSummary::test_summary_two_arg_infers_all_time_when_range_mode_omitted ... tests/test_docker_lifecycle.py --no-cov` initially failed on direct service all-time inference (`1 != 4`) before the production change; Docker lifecycle initially raced before the DB file existed and was fixed with a bounded wait. GREEN evidence: `pytest tests/test_dashboard.py tests/test_dashboard_api.py tests/test_web_phase3.py tests/test_seed_demo.py tests/test_sqlite_ops.py tests/test_dashboard_selection.py tests/test_config.py tests/test_documentation.py tests/test_docker_lifecycle.py --no-cov` → 102 passed in 26.61s | `python -m app.cli.seed_demo` twice on `/tmp/opencode` DB succeeded; `sqlite_ops backup` and `restore` reported integrity `ok` and zero count deltas; `uvicorn app.main:create_app --factory` + `curl /dashboard` returned HTTP 200; disposable Docker lifecycle test passed with project `finhealth-p7-<uuid8>` and isolated bind mounts | Revert Phase 7 changes in `app/services/dashboard.py`, `app/api/v1/dashboard.py`, `tests/test_dashboard.py`, `tests/test_dashboard_api.py`, `tests/test_seed_demo.py`, `tests/test_config.py`, `tests/test_sqlite_ops.py`, `tests/test_documentation.py`, `tests/test_docker_lifecycle.py`, `README.md`, and `scripts/verify.sh`; keep Phases 1–6 intact |
| WU-6 Phase 8 evidence/behavior completion | RED evidence: initial Phase 8 RED run failed before production changes on missing `copy_stopped_container_db`; collision/comment/spec/documentation tests were added before GREEN changes. GREEN evidence: `pytest tests/test_seed_demo.py tests/test_dashboard.py tests/test_sqlite_ops.py tests/test_docker_lifecycle.py tests/test_documentation.py tests/test_dashboard_api.py tests/test_web_phase3.py tests/test_dashboard_selection.py tests/test_config.py --no-cov` → **114 passed in 101.74s** | Runtime harness: `python -m app.cli.seed_demo` twice, `sqlite_ops backup`, `sqlite_ops restore`, and `copy_stopped_container_db` against `/tmp/opencode/finhealth-p8-runtime.*` → **passed**, stopped-copy `PRAGMA integrity_check` returned `ok`; `pytest tests/test_docker_lifecycle.py --no-cov` → **4 passed in 93.59s** with isolated `finhealth-p8-<uuid8>` projects/temp bind mounts | Revert Phase 8 changes in `app/cli/seed_demo.py`, `app/cli/sqlite_ops.py`, `tests/test_seed_demo.py`, `tests/test_sqlite_ops.py`, `tests/test_docker_lifecycle.py`, `tests/test_documentation.py`, `tests/test_dashboard.py`, `app/web/templates/partials/dashboard_summary.html`, `README.md`, `openspec/specs/phase3-dashboard/spec.md`, `tasks.md`, and `verify-report.md`; keep Phases 1–7 intact |

## Verification Evidence

- `pytest tests/test_dashboard.py tests/test_dashboard_api.py tests/test_web_phase3.py tests/test_seed_demo.py tests/test_sqlite_ops.py tests/test_dashboard_selection.py --no-cov` — **85 passed in 4.55s**.
- `pytest tests/test_recurring.py tests/test_dashboard.py::TestRecurring tests/test_dashboard_api.py::TestDashboardRecurring tests/test_web_phase3.py::test_dashboard_recurring_partial_returns_active_rules --no-cov` — **39 passed in 1.74s**.
- `ruff check app tests` — **All checks passed**.
- `python -m compileall -q app tests` — **passed; empty output**.
- Runtime harness: `python -m app.cli.seed_demo` twice, `python -m app.cli.sqlite_ops backup`, `python -m app.cli.sqlite_ops restore`, and `uvicorn app.main:create_app --factory` + `curl /dashboard` — **passed** (`/dashboard` HTTP 200; restore post-verification integrity `ok`).
- `pytest tests/test_dashboard.py tests/test_dashboard_api.py tests/test_web_phase3.py tests/test_seed_demo.py tests/test_sqlite_ops.py tests/test_dashboard_selection.py tests/test_config.py tests/test_documentation.py tests/test_docker_lifecycle.py --no-cov` — **102 passed in 26.61s**.
- `pytest tests/test_recurring.py tests/test_dashboard.py::TestRecurring tests/test_dashboard_api.py::TestDashboardRecurring tests/test_web_phase3.py::test_dashboard_recurring_partial_returns_active_rules --no-cov` — **39 passed in 1.78s**.
- `ruff check app tests` — **All checks passed**.
- `python -m compileall -q app tests` — **passed; empty output**.
- `out_dir=$(mktemp -d /tmp/opencode/finhealth-wheel-p7.XXXXXX) && python -m pip wheel . --no-deps -w "$out_dir"` — **built `finhealth-0.1.0-py3-none-any.whl` successfully**.
- `docker compose config -q` — **passed; empty output**.
- `docker compose -f docker-compose.self-hosted.yml config -q` — **passed; empty output**.
- `pytest tests/test_documentation.py tests/test_docker_lifecycle.py --no-cov` — **3 passed in 21.29s**.
- Runtime harness: `python -m app.cli.seed_demo` twice, `python -m app.cli.sqlite_ops backup`, `python -m app.cli.sqlite_ops restore`, and `uvicorn app.main:create_app --factory` + `curl /dashboard` — **passed** (`/dashboard` HTTP 200; restore post-verification integrity `ok`, counts delta zero).
- `pytest` — **24 failed, 585 passed, 57 warnings in 93.17s**. The failures match the known unrelated baseline domains and remain out of scope.
- `pytest tests/test_seed_demo.py tests/test_dashboard.py tests/test_sqlite_ops.py tests/test_docker_lifecycle.py tests/test_documentation.py tests/test_dashboard_api.py tests/test_web_phase3.py tests/test_dashboard_selection.py tests/test_config.py --no-cov` — **114 passed in 101.74s**.
- `pytest tests/test_docker_lifecycle.py --no-cov` — **4 passed in 93.59s** with unique disposable Compose project names and temporary bind mounts.
- `ruff check app tests` — **All checks passed**.
- `python -m compileall -q app tests` — **passed; empty output**.
- Runtime harness: `python -m app.cli.seed_demo` twice, `sqlite_ops backup`, `sqlite_ops restore`, and `copy_stopped_container_db` under `/tmp/opencode/finhealth-p8-runtime.*` — **passed** (`PRAGMA integrity_check` returned `ok`).
- `pytest` — **24 failed, 597 passed, 74 warnings in 162.18s**. The failures match the known unrelated baseline domains and remain out of scope.

## Deviations

- The existing schema has no dedicated provenance column for `Bank`, `CreditCard`, `Merchant`, `Category`, `Statement`, or `RecurringRule`; Phase 6 stores the seed marker in the least-invasive existing text/JSON field for each seed-created row type and keeps user-owned rows unmodified.
- `/health` is not an existing route in this app; restore health verification is implemented at the existing CLI/SQLite boundary via `PRAGMA integrity_check`, `PRAGMA quick_check`, and manifest count comparison. The runtime web smoke used `/dashboard` HTTP 200.
- Phase 7 adds disposable Docker lifecycle evidence with an isolated Compose project and temporary bind mounts; it never uses the developer's default Compose project or default `./data` directory.
- Direct service `range_months in {3,6,12}` without `range_mode` preserves legacy selected-month aggregation to avoid changing existing web partial behaviour. Direct `range_months=0` now infers all-time; explicit `range_mode` remains authoritative.
- Phase 8 Docker lifecycle and restored-DB tests use copied/generated Compose files with unique `finhealth-p8-<uuid8>` project names and temporary bind mounts only; no default Compose project or repository `./data` was touched.
- Phase 8 canonical spec sync updated only `openspec/specs/phase3-dashboard/spec.md` from the live hardening MODIFIED delta; archived history was not edited.

## Known Out-of-Scope Baseline Failures

- The unrelated full-suite baseline failures remain out of scope: 16 LLM Zen/OpenCode expectations, 3 ingestion failures, 3 real-PDF/e2e failures, 1 lifespan unreachable-path failure, and 1 config CORS environment-leakage failure.
- No remediation in this batch changed `app/services/recurring_detection.py`.

## Current State

- 97/97 tasks are complete in `tasks.md`.
- No commits or PRs were created by apply.
- `.codegraph/` is a local generated index only and must remain out of review/commit artifacts.
