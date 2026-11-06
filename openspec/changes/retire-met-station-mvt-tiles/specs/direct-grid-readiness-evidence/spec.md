## MODIFIED Requirements

### Requirement: Real-object-store and real-DB smoke on node-27

The platform SHALL run a direct-grid smoke against a real object store and real database on node-27, using a dedicated synthetic direct-grid evidence contract,
to confirm the pinned release behaves correctly outside in-memory fixtures without touching any production model instance or the production display.

#### Scenario: Node-27 smoke exercises real backends

- **WHEN** the readiness smoke is executed on node-27
- **THEN** it reads canonical evidence-contract inputs from the real filesystem-backed evidence-package path on node-27
  (`openspec/changes/cmfd-direct-grid-platform-readiness/evidence/synthetic-package/` under the deployed checkout — the real disk the deployment reads canonical
  evidence inputs from; NOT `/home/ghdc/nwm/object-store/{forcing,models}` which are production forcing-time-series roots read-only to the smoke's user role and
  out-of-scope for evidence writes)
- **THEN** the derived rows the smoke depends on are present in the real database (either written by the same `§2.4` smoke run, OR written by the paired `§2.3`
  evidence-only registration transaction with `§2.4` attesting a byte-identical read-back roundtrip and confinement to the dedicated non-production identity)
- **THEN** it confirms direct-grid production does not fall back to legacy IDW station loading (structural argument citing scheduler-dispatch filter + producer
  station-loader filter + runtime path selection is acceptable when the smoke intentionally does not execute a live pipeline pass against the evidence identity)
- **THEN** it records the live database's applied schema migration version and confirms it equals the manifest's pinned `db_schema_migration_version`
- **THEN** the smoke evidence records the node-27 host, the pinned `baseline_commit`, and the manifest checksum; when the code carrier commit at run time
  differs from the pinned `baseline_commit` (because an evidence carrier landed after manifest freeze), the smoke evidence additionally records the
  `code_carrier_sha` and asserts empty tree-diff between the two commits on all manifest-identity paths (`workers/`, `apps/`, `services/`, `packages/`, `db/`,
  `schemas/`).

#### Scenario: Smoke runs against the synthetic evidence contract with isolation and cleanup

- **WHEN** the node-27 smoke's `§2.3`+`§2.4` pair provisions its direct-grid contract and writes derived rows to the real database (whether directly in the `§2.4` smoke or via the paired `§2.3` evidence-only registration transaction that the `§2.4` smoke attests read-back for)
- **THEN** it uses the hand-assembled synthetic direct-grid evidence contract registered as a dedicated evidence-only `core.model_instance` row carrying `resource_profile.direct_grid_forcing`, with construction provenance and SHA-256 checksums recorded in the evidence
- **THEN** it does not read its contract from, or write derived rows against, any of the 13 live production model instances
- **THEN** derived rows are confined to a dedicated non-production identity (dedicated `basin_version_id`/`model_id`) and any `met.met_station` mirror rows carry `active_flag=false`, so the active station set cannot mix old/new stations
- **THEN** after evidence capture the smoke's derived and mirror rows are removed, or verifiably remain confined to the inactive dedicated identity, and a display spot-check confirms production display is unaffected.
