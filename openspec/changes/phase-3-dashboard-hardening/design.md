# Design: Phase 3 Dashboard Hardening

## Technical Approach

Keep FastAPI/Jinja2 as adapters around SQLAlchemy services. Add a dependency-free selection/range module; one web composition resolves selection once for all five sections. Preserve JSON contracts while hardening demo data and SQLite-only operations. PostgreSQL remains outside this change.

## Architecture Decisions

| Option | Tradeoff | Decision and SOLID rationale |
|---|---|---|
| Parse range in routes/templates | Duplicates semantics and caused `0` ambiguity | `dashboard_selection.py` owns immutable selection, typed modes, labels, API translation, and a pure resolver accepting injected `today` and earliest card-filtered date. SRP/OCP keeps policy independently testable. |
| Refresh five endpoints/hand-build URLs | More requests and divergent state | One `GET /dashboard/sections` receives one HTMX form, builds one view, and renders KPI, categories, merchants, monthly, and recurring. DIP keeps Jinja independent of queries. |
| Identify seed rows by mutable fields | Can overwrite user data | Fixed UUIDv5 namespace/keys own every created row; statement hash and transaction JSON expose provenance. Reconcile by ID only; natural-key conflicts roll back. No deletes. |
| Copy a live SQLite file | WAL may hold committed pages | A thin CLI uses Python `sqlite3` backup API, temporary files, `integrity_check`, count manifests, and atomic replacement. Reject non-SQLite URLs. |

## Data Flow

```text
HTTP query → selection parser → earliest-date query → pure window resolver
           → DashboardService.compose → DashboardView → Jinja full/HTMX partial
API range=0 → API translator → all_time (public response remains compatible)
```

`summary` aggregates `(currency, sum, count)`, derives additive counts, and divides totals by `calendar.monthrange(period)[1]`. Previous-month comparison stays independent of the history window.

## File Changes

| File | Action | Description |
|---|---|---|
| `app/services/dashboard_selection.py` | Create | Pure value objects, labels, API mode translation, resolver. |
| `app/services/dashboard.py`, `app/schemas/dashboard.py`, `app/api/v1/dashboard.py` | Modify | Composition, calendar denominator/counts, `range=0` mapping. |
| `app/web/router.py`, `app/web/templates/dashboard.html`, partials `dashboard_summary.html`, `dashboard_categories.html`, `dashboard_merchants.html`, `dashboard_monthly.html`, `dashboard_recurring.html` | Modify | One active-card HTMX form, dynamic labels/counts; remove anomalies. |
| `app/web/templates/partials/dashboard_sections.html` | Create | Atomic five-section HTMX response. |
| `app/web/templates/partials/dashboard_anomalies.html` | Delete | No detector means no panel or availability claim. |
| `app/cli/seed_demo.py` | Modify | Normalized aliases and ownership-only atomic upsert. |
| `app/cli/sqlite_ops.py` | Create | Explicit `backup`/`restore` CLI adapter without shell composition. |
| `app/core/config.py`, `.env.example`, `docker-compose*.yml`, `README.md` | Modify | Canonical `data/finhealth.db`, bind-mount truth, verified runbook. |
| `tests/test_dashboard.py`, `tests/test_dashboard_api.py`, `tests/test_web_phase3.py`, `tests/test_config.py` | Modify | Compatibility coverage. |
| `tests/test_dashboard_selection.py`, `tests/test_seed_demo.py`, `tests/test_sqlite_ops.py` | Create | Policy, ownership, WAL tests. |

## Interfaces / Contracts

`DashboardSelection(period: YearMonth, card_id: UUID | "all", range_mode: Rolling | YTD | AllTime)` resolves to inclusive dates. Empty all-time data resolves from the selected month start. `SummaryResponse` adds `transaction_count_per_currency: dict[str, int]`; existing fields and endpoint query parameters remain stable.

Backup writes a verified database plus count manifest. Restore requires a stopped app, validates before mutation, rejects a busy destination, removes stale `-wal`/`-shm`, replaces atomically, then verifies endpoints/counts.

## Testing Strategy

| Layer | What to Test | Approach |
|---|---|---|
| Unit | modes/leap years; aliases; ownership | Parametrized July `/31` and Unicode cases. |
| Integration | counts/`range=0`; seed reruns; backup/restore | WAL SQLite; assert rollback, integrity, counts, untouched rows. |
| Web | active cards, labels, one form/swap, five sections, no anomaly claim | `httpx` HTML assertions using existing FastAPI fixtures. |

## Threat Matrix

| Boundary / cases | Applicability | Safe / failure behavior | Planned RED test |
|---|---|---|---|
| Documentation-like paths (`requirements.txt`, `CMakeLists.txt`, executable MDX, `README.sh`) | N/A — no classification | Documentation is never dispatched | None |
| Git repository selection (`git -C`, relative, absolute) | N/A — no VCS command | No Git invocation | None |
| Commit state (staged, `-a`, empty index) | N/A — no commits | No index access | None |
| Push state (tracking, first push, refspec) | N/A — no push | No remote access | None |
| PR commands (`--head`, env prefix, composition) | N/A — no PR automation | No command composition | None |
| Dashboard routing: malformed period/card/mode | Applicable | Canonical selection or HTTP 400; preserve `range=0` | Invalid-value and `0 → all_time` RED tests |
| Seed: rerun, key collision, user statement | Applicable | Owned IDs only; conflict rolls back/non-zero | Identity and preservation RED tests |
| Backup/restore: writer, same path, corruption, PostgreSQL URL | Applicable | Verified temp/atomic replace; reject before mutation | WAL, busy, path, corruption, non-SQLite RED tests |

## Migration / Rollout

No schema migration. Verify a backup first; deploy together, seed optionally, and smoke-test JSON/HTMX. Roll back together and restore the snapshot if needed.

## Open Questions

None.
