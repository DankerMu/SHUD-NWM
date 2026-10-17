# Spec Delta

## Purpose
No live code or active runbook/spec treats the dropped `hydro.river_timeseries_legacy` table or the removed `timeseries_store` column as current.

## ADDED Requirements

### Requirement: Live code SHALL NOT reference the dropped legacy table
`packages/common/node27_pgdata_workload_plan.py` SHALL NOT define `LEGACY_HYPERTABLE`, SHALL NOT include `hydro.river_timeseries_legacy` in any executed SQL list, its default `expected_relations` SHALL NOT name it, and its candidate loader SHALL treat a candidate whose parent is `river_timeseries_legacy` as an unrelated parent (`PLAN_UNRELATED_PARENT`), since the table was dropped on 2026-09-19; the existing loader test that feeds such a candidate and expects success SHALL be rewritten to expect the refusal (an allowed test edit). Catalog-driven discovery in `node27_timeseries_discovery.py` may keep deriving the `_legacy` name only when the catalog contains it. A test SHALL assert the string `river_timeseries_legacy` occurs in non-test Python under `apps/ packages/ services/ workers/ scripts/` only inside comments, docstrings, or the operator advice text `packages/common/display_coverage.py::_REFUSAL_ADVICE` (historical context, allowlisted by name).

#### Scenario: Workload plan on a catalog without the legacy table
- **WHEN** the workload plan runs against a catalog that has only `hydro.river_timeseries`
- **THEN** it reports no missing relation

#### Scenario: Legacy candidate is refused
- **WHEN** the candidate loader receives a row whose `hypertable_name` is `river_timeseries_legacy`
- **THEN** it refuses with `PLAN_UNRELATED_PARENT`

### Requirement: Active runbooks and specs SHALL mark legacy-store text as historical
`docs/runbooks/tier-node27-timeseries-storage.md`, `docs/runbooks/production-ops/service-bringup.md`, `docs/adr/0002-node27-timeseries-hot-cold-tiering.md` and `openspec/specs/{node27-window-admission,autopipe-parse-failure-residency,hypertable-compression,node27-river-narrow-reparse-backfill,forecast-api}/spec.md` SHALL either rewrite legacy-store and routing-column sentences to past tense with a pointer to the glossary entries, or wrap them in a complete DOC_STATUS section marker.

#### Scenario: Governance gate accepts the markers
- **WHEN** `uv run pytest -q tests/test_entropy_audit_report_contract.py` and `openspec validate --specs --no-interactive` run (non-strict: 201 pre-existing specs fail `--strict` on HEAD for missing scenarios, unrelated to this change)
- **THEN** both pass and no active runbook instructs an operator to read `timeseries_store`
