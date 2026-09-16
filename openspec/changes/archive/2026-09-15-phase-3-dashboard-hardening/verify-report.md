```yaml
schema: gentle-ai.verify-result/v1
evidence_revision: sha256:ed09f5b2caf9db415cbb95505169032b123a6e801e6cb307d3215e82b921fe4c
verdict: pass
blockers: 0
critical_findings: 0
requirements: 15/15
scenarios: 35/35
test_command: pytest tests/test_seed_demo.py tests/test_dashboard.py tests/test_sqlite_ops.py tests/test_docker_lifecycle.py tests/test_documentation.py tests/test_dashboard_api.py tests/test_web_phase3.py tests/test_dashboard_selection.py tests/test_config.py --no-cov
test_exit_code: 0
test_output_hash: sha256:ce050de20dec9eacad4c9137e567d35fd65689cd6fa1ebdd862574bf7a4bb36d
build_command: out_dir=$(mktemp -d /tmp/opencode/finhealth-wheel-final.XXXXXX) && python -m pip wheel . --no-deps -w "$out_dir"
build_exit_code: 0
build_output_hash: sha256:efe47f88cd0dadde32b174158ef7702b1bb8566238f09175badcaca81bea9ff7
```

## Verification Report

**Change**: `phase-3-dashboard-hardening`
**Version**: N/A
**Mode**: Standard, legacy-compatible final verification (`strict_tdd: false`)
**Persistence**: Hybrid (OpenSpec + Engram)
**Verification date**: 2026-07-17
**Final verdict**: **PASS WITH WARNINGS**

The change scope passes: all 97 tasks are complete, all 15 requirements and 35 scenarios have passing runtime coverage, focused and Docker suites are green, and all requested build/static checks pass. The only warnings are the unchanged 24-failure repository baseline and the native modern archive router's incompatibility with the user's explicit legacy-compatible, no-review-metadata decision.

### Native Status and Artifact Completeness

| Item | Result |
|---|---|
| Native task progress | **97/97 complete**, 0 pending |
| OpenSpec proposal/specs/design/tasks/apply-progress/verify-report | Present |
| Engram proposal/spec/design/tasks/apply-progress/verify-report topics | Present |
| Action context | Repo-local; allowed edit root is `/home/tadashi/develop/finhealth` |
| Review metadata | Intentionally absent and not required, created, modified, or retried under the user's legacy-compatible decision |

After this report was persisted, read-only native status reported proposal/specs/design/tasks/apply/verify all done, **97/97 tasks complete**, and `verify: all_done`. It still reports `archive: blocked` / `nextRecommended: resolve-review` because modern review receipt metadata is absent or invalidated. That archive-routing warning is documented rather than remediated: this report intentionally does not create, modify, retry, or require review metadata.

### Completeness

| Metric | Value |
|---|---:|
| Requirements | 15/15 |
| Scenarios | 35/35 |
| Tasks total | 97 |
| Tasks complete | 97 |
| Tasks incomplete | 0 |

### Build, Static Analysis, and Test Execution

| Command | Exit | Result | Output SHA-256 |
|---|---:|---|---|
| `pytest tests/test_seed_demo.py tests/test_dashboard.py tests/test_sqlite_ops.py tests/test_docker_lifecycle.py tests/test_documentation.py tests/test_dashboard_api.py tests/test_web_phase3.py tests/test_dashboard_selection.py tests/test_config.py --no-cov` | 0 | **114 passed in 101.48s** | `ce050de20dec9eacad4c9137e567d35fd65689cd6fa1ebdd862574bf7a4bb36d` |
| `pytest tests/test_docker_lifecycle.py --no-cov` | 0 | **4 passed in 93.78s** | `51a5ef4286e34250beeec87c21ff0a2552ae71c8c3c5674ca31a920051373c1c` |
| `pytest tests/test_recurring.py tests/test_dashboard.py::TestRecurring tests/test_dashboard_api.py::TestDashboardRecurring tests/test_web_phase3.py::test_dashboard_recurring_partial_returns_active_rules --no-cov` | 0 | **39 passed in 1.75s** | `266d199a6f45f4205eef085db3cda16297d5b0880629774d00c3c4a156a5afe4` |
| `ruff check app tests` | 0 | All checks passed | `82b3e6a6c090a57601d22943bd23fca9218d1031dbe5a7b754092f9a156b4f18` |
| `python -m compileall -q app tests` | 0 | Passed; empty output | `e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855` |
| Isolated `python -m pip wheel . --no-deps` build | 0 | Built `finhealth-0.1.0-py3-none-any.whl` | `efe47f88cd0dadde32b174158ef7702b1bb8566238f09175badcaca81bea9ff7` |
| `docker compose config -q && docker compose -f docker-compose.self-hosted.yml config -q` | 0 | Both Compose configurations valid; empty output | `e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855` |
| `scripts/verify.sh` | 0 | Focused **114 passed**; docs/Docker **8 passed**; Ruff and compileall passed | `70aa631f6b912b5e2d5e1c3694cdbe14e9056c19d1bfa33b0196551a50d042d5` |
| `pytest` | 1 | **24 failed, 597 passed, 75 warnings in 168.94s**; failures are the unrelated baseline listed below | `a6a4d73b49216816ccd98bb949085c245879eb5ef73bd73b6d28e5c778b4a720` |

**Coverage observation**: the full repository run reported **86.17%** total coverage. No project coverage threshold is configured. Coverage is informational because the repository-wide run contains the known unrelated failures.

### Spec Compliance Matrix

| # | Requirement | Scenario | Passing runtime evidence | Result |
|---:|---|---|---|---|
| 1 | Summary KPI aggregates | Single-currency period | `tests/test_dashboard.py::TestSummary::test_summary_returns_kpis_for_period` | ✅ COMPLIANT |
| 2 | Summary KPI aggregates | Multi-currency period | `tests/test_dashboard.py::TestSummary::test_summary_multi_currency_sub_rollup` | ✅ COMPLIANT |
| 3 | Summary KPI aggregates | Single-card filter | `tests/test_dashboard.py::TestSummary::test_summary_with_card_id_all_vs_uuid` | ✅ COMPLIANT |
| 4 | Summary KPI aggregates | `range=0` is all-time | `tests/test_dashboard.py::TestSummary::test_summary_range_zero_returns_all_time_with_history`; `test_summary_two_arg_infers_all_time_when_range_mode_omitted` | ✅ COMPLIANT |
| 5 | Summary KPI aggregates | Empty period | `tests/test_dashboard.py::TestSummary::test_summary_empty_period_returns_zero_counts_and_empty_per_currency` | ✅ COMPLIANT |
| 6 | Dashboard page | Full layout | `tests/test_web_phase3.py::test_dashboard_page_returns_200` | ✅ COMPLIANT |
| 7 | Dashboard page | Active-only card picker | `tests/test_web_phase3.py::test_dashboard_card_picker_excludes_inactive_cards` | ✅ COMPLIANT |
| 8 | Dashboard page | Period picker modes | `tests/test_web_phase3.py::test_dashboard_page_contains_period_picker` | ✅ COMPLIANT |
| 9 | Dashboard page | Single HTMX form refreshes all sections | `tests/test_web_phase3.py::test_dashboard_single_htmx_form_refreshes_all_sections` | ✅ COMPLIANT |
| 10 | Selection/resolver | YTD window | `tests/test_dashboard_selection.py::test_resolver_ytd_all_time_and_api_zero_mapping` | ✅ COMPLIANT |
| 11 | Selection/resolver | All-time window | `tests/test_dashboard_selection.py::test_resolver_ytd_all_time_and_api_zero_mapping` | ✅ COMPLIANT |
| 12 | Selection/resolver | API `range=0` compatibility | `tests/test_dashboard_api.py::TestDashboardSummary::test_summary_range_zero_service_receives_all_time_inference`; `test_summary_range_zero_reaches_resolver_with_all_time` | ✅ COMPLIANT |
| 13 | Live currency counts | USD count derived | `tests/test_web_phase3.py::test_dashboard_summary_renders_usd_count_from_payload` | ✅ COMPLIANT |
| 14 | Truthful anomaly state | No anomaly panel | `tests/test_web_phase3.py::test_dashboard_single_htmx_form_refreshes_all_sections` | ✅ COMPLIANT |
| 15 | Alias normalization | Normalized alias resolves | `tests/test_seed_demo.py::test_category_alias_normalization[dining-Dining Out]`; Unicode/case parameter | ✅ COMPLIANT |
| 16 | Alias normalization | Unknown key falls back | `tests/test_seed_demo.py::test_category_alias_normalization[something_new-Uncategorized]` | ✅ COMPLIANT |
| 17 | Seed provenance | Seed rows carry marker | `tests/test_seed_demo.py::test_every_seed_row_carries_provenance_marker` | ✅ COMPLIANT |
| 18 | Seed provenance | Non-seed rows stay unmarked | `tests/test_seed_demo.py::test_user_row_marker_unchanged_after_seed` | ✅ COMPLIANT |
| 19 | Repeat-safe upsert | Two runs yield identical seed-owned rows | `tests/test_seed_demo.py::test_two_runs_full_mapped_column_snapshot` | ✅ COMPLIANT |
| 20 | Repeat-safe upsert | User rows survive | `tests/test_seed_demo.py::test_two_runs_user_full_value_equality` | ✅ COMPLIANT |
| 21 | Repeat-safe upsert | Seed never attaches to user statements | `tests/test_seed_demo.py::test_seed_is_repeat_safe_and_preserves_user_rows` | ✅ COMPLIANT |
| 22 | Seed-owned statements/transactions | Stable key lookup | `tests/test_seed_demo.py::test_exact_id_collision_rejected_for_every_seed_entity`; `test_exact_file_hash_collision_rejected_for_statement` | ✅ COMPLIANT |
| 23 | Seed-owned statements/transactions | Mutable attributes alone do not reconcile | `tests/test_seed_demo.py::test_seed_collision_user_statement_same_card_period` | ✅ COMPLIANT |
| 24 | Uncategorized behavior | Zero spend | `tests/test_dashboard.py::TestCategories::test_uncategorized_end_to_end_through_seed` | ✅ COMPLIANT |
| 25 | Uncategorized behavior | Seed spend | `tests/test_seed_demo.py::test_unknown_alias_seeds_two_uncategorized_dashboard_rows` | ✅ COMPLIANT |
| 26 | Canonical SQLite path | Local default | `tests/test_config.py::test_default_database_url_is_canonical_data_path` | ✅ COMPLIANT |
| 27 | Canonical SQLite path | Docker default | `tests/test_documentation.py::test_compose_bind_mount_uses_data_directory` | ✅ COMPLIANT |
| 28 | Bind-mount persistence | Exact `down` then `up` preserves rows | `tests/test_docker_lifecycle.py::test_disposable_compose_down_up_persists_data` | ✅ COMPLIANT |
| 29 | Bind-mount persistence | `down -v` preserves host data | `tests/test_docker_lifecycle.py::test_disposable_compose_down_dash_v_preserves_host_data` | ✅ COMPLIANT |
| 30 | Verified backup | SQLite backup API | `tests/test_sqlite_ops.py::test_backup_passes_integrity_and_writes_manifest` | ✅ COMPLIANT |
| 31 | Verified backup | Stopped-container copy | `tests/test_sqlite_ops.py::test_stopped_container_backup_copy_passes_integrity` | ✅ COMPLIANT |
| 32 | Verified restore | Counts and dashboard HTTP 200 | `tests/test_sqlite_ops.py::test_restore_post_verification_counts_and_integrity`; `tests/test_docker_lifecycle.py::test_restored_database_serves_dashboard_http_200` | ✅ COMPLIANT |
| 33 | Verified restore | Integrity failure aborts | `tests/test_sqlite_ops.py::test_restore_rejects_corrupt_and_non_sqlite_before_mutation` | ✅ COMPLIANT |
| 34 | Documentation accuracy | README does not claim `-v` deletes host data | `tests/test_documentation.py::test_readme_docker_runbook_claims`; `test_readme_documents_stopped_container_backup` | ✅ COMPLIANT |
| 35 | Documentation accuracy | Backup/restore runbook present | `tests/test_documentation.py::test_readme_docker_runbook_claims` | ✅ COMPLIANT |

**Compliance summary**: **35/35 scenarios compliant at runtime** across **15/15 requirements**.

### Correctness (Static Evidence)

| Requirement | Status | Static evidence |
|---|---|---|
| Summary KPI aggregates | ✅ Implemented | `DashboardService.summary` validates range, infers direct `range_months=0` as all-time, preserves explicit-mode precedence, uses calendar-month days, and returns per-currency counts. |
| Dashboard page | ✅ Implemented | One server-side selection form targets `/dashboard/sections`; all five sections render atomically; active cards and dynamic labels are server-derived. |
| Selection value object/resolver | ✅ Implemented | `dashboard_selection.py` owns immutable period/card/range policy, API translation, dynamic labels, and pure date-window resolution. |
| Live per-currency counts | ✅ Implemented | `SummaryResponse.transaction_count_per_currency` is populated by grouped SQL and rendered by the summary partial. |
| Truthful anomaly state | ✅ Implemented | The anomaly partial was removed and is absent from the unified section composition. |
| Category alias normalization | ✅ Implemented | Case-folded Unicode normalization maps aliases to the closed set and unknown keys to `Uncategorized`. |
| Deterministic seed provenance | ✅ Implemented | Stable UUIDv5 keys and marker-bearing fields identify all seed-created entity types. |
| Repeat-safe non-destructive upsert | ✅ Implemented | Reconciliation is ownership-scoped; no table-wide delete exists; full mapped snapshots and user-row equality pass. |
| Seed-owned statements/transactions | ✅ Implemented | Exact deterministic IDs/hashes are checked before mutation; unmarked collisions raise and roll back. |
| Uncategorized behavior | ✅ Implemented | Seed creates the closed-set row; unknown aliases aggregate under it in the dashboard. |
| Canonical SQLite path | ✅ Implemented | Local default is `data/finhealth.db`; both Compose files use `/app/data/finhealth.db`. |
| Bind-mount persistence | ✅ Implemented | Both Compose files bind `./data:/app/data`; runtime tests execute exact `down`/`up` and `down -v`. |
| Verified backup | ✅ Implemented | Backup API and stopped-container copy paths run integrity checks and preserve WAL/SHM handling. |
| Verified restore | ✅ Implemented | Restore rejects busy/corrupt/non-SQLite inputs, removes stale sidecars, atomically replaces, then verifies integrity/counts; restored DB serves HTTP 200. |
| Documentation accuracy | ✅ Implemented | README contains canonical path, bind-mount semantics, exact lifecycle, stopped-copy command, restore steps, and verification guidance. |

### Coherence (Design)

| Decision | Followed? | Notes |
|---|---|---|
| Centralize selection/range policy | ✅ Yes | `dashboard_selection.py` owns parsing, typed modes, labels, API translation, and resolver policy. |
| One HTMX composition for five sections | ✅ Yes | `/dashboard/sections` builds one context and returns `dashboard_sections.html`. |
| Seed ownership by deterministic identity | ✅ Yes | UUIDv5/file-hash identity and pre-mutation collision guards replace mutable-key reconciliation. |
| Use SQLite backup API and verified atomic restore | ✅ Yes | Temporary snapshots, integrity checks, count manifests, busy guard, sidecar cleanup, and atomic replace are present. |
| Preserve API compatibility | ✅ Yes | API `range=0` and direct service `range_months=0` both resolve to all-time; explicit `range_mode` remains authoritative. |
| Keep archive audit history immutable | ✅ Yes | Only live `openspec/specs/phase3-dashboard/spec.md` was synced; archived history was not modified. |

### Phase 8 Special-Attention Findings

- **Full seed snapshots and collision safety**: full non-timestamp mapped-column snapshots pass for every seed-owned row type; user `Transaction` mapped values remain identical after two runs; exact deterministic ID and statement-hash collisions fail before mutation.
- **Unknown alias to dashboard `Uncategorized`**: two unknown aliases seed two CLP rows and aggregate to `CLP 300.00`, count `2`, under the marked `Uncategorized` category.
- **Exact Docker lifecycle semantics**: isolated tests execute `docker compose down` then `up`, and separately `down -v`; the host bind-mounted database survives both.
- **Stopped-container copy**: `copy_stopped_container_db` copies the DB plus optional WAL/SHM sidecars and verifies the destination with `PRAGMA integrity_check`.
- **Restored database HTTP smoke**: a disposable real FinHealth container serves the summary endpoint before and after restore with matching transaction counts and HTTP 200.
- **Stale comment/spec sync**: the obsolete hard-coded recurring narrative is absent; the live canonical spec uses the calendar-day denominator and archived history remains untouched.
- **Direct/API `range=0`**: direct service calls infer all-time, API calls forward `RangeMode.all_time()`, selected-month comparison remains anchored to the previous month, and daily average remains divided by the selected month's calendar days.

### Known Unrelated Full-Suite Baseline Failures

The repository-wide `pytest` command remains red with the same **24 unrelated baseline failures**. They are recorded separately and are not attributed to this change:

| Baseline domain | Failures | Scope assessment |
|---|---:|---|
| LLM Zen/OpenCode schema, endpoint, header, response-block, retry, and helper expectations | 16 | Outside dashboard hardening |
| Ingestion | 3 | Outside dashboard hardening |
| Real-PDF/e2e | 3 | Outside dashboard hardening |
| Lifespan unreachable-path behavior | 1 | Outside dashboard hardening |
| Suite-order configuration environment leakage (`CORS_ORIGINS`) | 1 | Outside change; focused config tests pass |

No full-suite failure is in dashboard selection/service/API/web, demo seeding, SQLite operations, documentation, Docker lifecycle, or recurring regressions.

### Issues Found

**CRITICAL**: None.
**WARNING**:
1. The unrelated repository baseline remains 24 failures, so repository-wide `pytest` is not globally green.
2. Native status marks verification `all_done` but still recommends `resolve-review` before archive because review receipt metadata is absent or invalidated. This is intentionally not remediated under the explicit legacy-compatible decision; archive orchestration must preserve that decision rather than create review metadata.

**SUGGESTION**: Track and repair the 24 baseline failures independently from this change.

### Canonical Verification Evidence Preimage

The exact UTF-8 bytes between the following fence markers, including the final LF before the closing fence, hash to the strict-envelope `evidence_revision`.

```text
schema: gentle-ai.verification-evidence/v1
change: phase-3-dashboard-hardening
mode: standard-legacy-compatible
candidate_tree_hash: sha256:2d627766e64bfe98d47cc6d77391c5810b0d6ac734d7d902e7691cd4a2f1f4d0
tasks: 97/97
requirements: 15/15
scenarios: 35/35
focused_exit_code: 0
focused_output_hash: sha256:ce050de20dec9eacad4c9137e567d35fd65689cd6fa1ebdd862574bf7a4bb36d
docker_exit_code: 0
docker_output_hash: sha256:51a5ef4286e34250beeec87c21ff0a2552ae71c8c3c5674ca31a920051373c1c
recurring_exit_code: 0
recurring_output_hash: sha256:266d199a6f45f4205eef085db3cda16297d5b0880629774d00c3c4a156a5afe4
ruff_exit_code: 0
ruff_output_hash: sha256:82b3e6a6c090a57601d22943bd23fca9218d1031dbe5a7b754092f9a156b4f18
compileall_exit_code: 0
compileall_output_hash: sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855
wheel_exit_code: 0
wheel_output_hash: sha256:efe47f88cd0dadde32b174158ef7702b1bb8566238f09175badcaca81bea9ff7
compose_exit_code: 0
compose_output_hash: sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855
verify_script_exit_code: 0
verify_script_output_hash: sha256:70aa631f6b912b5e2d5e1c3694cdbe14e9056c19d1bfa33b0196551a50d042d5
full_pytest_exit_code: 1
full_pytest_output_hash: sha256:a6a4d73b49216816ccd98bb949085c245879eb5ef73bd73b6d28e5c778b4a720
baseline_failures: 24
change_scoped_failures: 0
review_metadata_required: false
review_metadata_mutated: false
```

### Verdict

**PASS WITH WARNINGS.** All 15 requirements and 35 scenarios are compliant with current runtime evidence, and every requested change-scoped/build check passes. The 24 unrelated repository baseline failures and legacy/native review-routing mismatch remain non-change-scoped warnings.
