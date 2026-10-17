# Spec Delta

## Purpose
No live code or active runbook/spec treats the dropped `hydro.river_timeseries_legacy` table or the removed `timeseries_store` column as current.

## ADDED Requirements

### Requirement: Live code SHALL NOT reference the dropped legacy table
`packages/common/node27_pgdata_workload_plan.py` SHALL NOT define `LEGACY_HYPERTABLE`, SHALL NOT include `hydro.river_timeseries_legacy` in any executed SQL list, and its default `expected_relations` SHALL NOT name it. Catalog-driven discovery in `node27_timeseries_discovery.py` may keep deriving the `_legacy` name only when the catalog contains it. A test SHALL assert the string `river_timeseries_legacy` occurs in non-test Python only inside comments or docstrings.

#### Scenario: Workload plan on a catalog without the legacy table
- **WHEN** the workload plan runs against a catalog that has only `hydro.river_timeseries`
- **THEN** it reports no missing relation

### Requirement: Active runbooks and specs SHALL mark legacy-store text as historical
`docs/runbooks/tier-node27-timeseries-storage.md`, `docs/runbooks/production-ops/service-bringup.md`, `docs/adr/0002-node27-timeseries-hot-cold-tiering.md` and the five `openspec/specs/*/spec.md` files named in the audit SHALL either rewrite legacy-store and routing-column sentences to past tense with a pointer to the glossary entries, or wrap them in a complete DOC_STATUS section marker.

#### Scenario: Governance gate accepts the markers
- **WHEN** `uv run pytest -q tests/test_entropy_audit_report_contract.py` runs
- **THEN** it passes and no active runbook instructs an operator to read `timeseries_store`
