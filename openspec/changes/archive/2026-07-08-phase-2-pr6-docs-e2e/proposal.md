# Proposal: Phase 2 PR #6 — Documentation & E2E Test

## Intent

Phase 2 is complete in main (PR #2–#5), but `README.md` still says "Phase 1" and the suite has no happy-path test proving the three new capabilities wire together. PR #6 closes that loop: one README update + one e2e test. No new code, no migrations, no schema changes.

## Scope

### In Scope

- `README.md` — Phase 2 section, 6 new endpoints, migrations `0005`–`0007`, recurring `confidence`.
- `tests/test_e2e_phase2.py` — Santander happy-path e2e: ingest → merchant + category + recurring → `GET`/`PATCH`.

### Out of Scope

New features, models, migrations, schema, LLM changes. Recurring UI (decision #12). CHANGELOG (project has none). Promoting `phase2-recurring-detection` spec (PR #5 archive step).

## Capabilities

### New Capabilities
None.

### Modified Capabilities
None. Docs + test artifact; no spec-level requirements change.

## Approach

**README** — Insert "Phase 2 — Classification" after "Available now (Phase 1)". One paragraph per capability + cross-reference to `openspec/specs/<name>/spec.md`. Add 6 new endpoints to the v1 table. Add migrations `0005`→`0007`. Update top-of-file "Status" from "Phase 1" to "Phase 1 + Phase 2".

**E2E test** — Mirror `tests/test_e2e_phase1.py`: same `FakeLLMClient` (canned NACIONAL with `SUPERMERCADOS LIDER`, `COMBUSTIBLE COPEC`, `PARIS`), same `SANTANDER_PDF`, same `needs_sample_pdf` + `needs_test_rut` markers, same fixtures. Pre-seed LIDER merchant + 2 historical txns on the same `credit_card_id` dated 60 + 30 days back — the canned 3rd LIDER row from the LLM then hits the detector's 3-occurrence threshold. Assertions: (1) upload 201 with `merchant_id` + `category_id` + `recurring_rule_id` populated; (2) `GET /api/v1/recurring` returns the LIDER rule, `period_label="monthly"`; (3) `PATCH {"is_active": false}` 200 + excluded from next GET; (4) FK on the three LIDER transactions preserved after deactivation (design D).

## Affected Areas

| Area | Impact | Description |
|------|--------|-------------|
| `README.md` | Modified | +Phase 2 section, +6 endpoints, +3 migrations, status note |
| `tests/test_e2e_phase2.py` | New | ~250 LOC, mirrors Phase 1 e2e + Phase 2 assertions |

## Risks

| Risk | Likelihood | Mitigation |
|------|------------|------------|
| Real PDF + `TEST_RUT` absent in CI; detector needs ≥3; one upload = 1 LIDER row | Med | `needs_*` skip markers (Phase 1 pattern) + pre-seed 2 historical LIDER txns on the same card (60d + 30d back) |
| `phase2-recurring-detection` spec not yet at `openspec/specs/`; README accuracy drift | Low | PR #5 archive step moves it; apply phase verifies path before opening PR; cross-check every claim against 3 specs + `app/api/v1/{categories,merchants,recurring}.py` |

## Rollback Plan

Revert the single commit. `README.md` is plain Markdown (no runtime impact); `tests/test_e2e_phase2.py` is a new test (delete on revert). No code paths, schema, or migrations touched — blast radius is zero.

## Dependencies

PR #5 archive step must run before PR #6 merges, or the README cross-reference to `openspec/specs/phase2-recurring-detection/spec.md` is broken. 3 Phase 2 specs in main (recurring pending archive); `tests/test_e2e_phase1.py` pattern; local `shared/account-state-examples/80_15796_0350262800062166708_20260422.pdf` (gitignored) + `TEST_RUT` env var.

## Success Criteria

- [ ] README "Phase 2" section covers all 3 capabilities; API table has 6 new endpoints; migrations `0005`–`0007` mentioned; status note updated to "Phase 1 + Phase 2".
- [ ] `tests/test_e2e_phase2.py` mirrors Phase 1 e2e style; passes locally with `TEST_RUT` set; asserts all 3 new FKs populated + `GET`/`PATCH` recurring + FK preserved on deactivation.
- [ ] `ruff check` + `ruff format` clean; total changed lines < 800.
