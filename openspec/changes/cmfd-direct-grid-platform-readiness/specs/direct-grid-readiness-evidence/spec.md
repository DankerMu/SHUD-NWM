## ADDED Requirements

### Requirement: Direct-grid test suites are re-run on the pinned release
The platform SHALL re-execute the direct-grid contract parser, producer, exact-cell value, standard SHUD package, runtime staging, out-of-range `.sp.att FORC` negative, idempotency, and DB migration test suites on the pinned readiness release, capturing pass evidence bound to the manifest checksum.

#### Scenario: All required suites pass on the pinned commit
- **WHEN** readiness evidence is generated for the pinned release
- **THEN** direct-grid contract parser tests pass
- **THEN** direct-grid producer tests and exact-cell value tests pass
- **THEN** standard SHUD package tests and runtime staging tests pass
- **THEN** out-of-range `.sp.att FORC` negative tests pass
- **THEN** idempotency tests pass
- **THEN** DB migration tests pass
- **THEN** the suites are executed on the node-27 deployment host at the pinned commit (per the verification-oracle routing that assigns backend pytest to node-27, and per the source-of-truth requirement that re-run evidence come from the actually deployed release, not a local checkout)
- **THEN** the evidence records the node-27 host, the pinned SHUD-NWM commit, and the readiness manifest checksum the run was executed against.

#### Scenario: Evidence maps to existing suites
- **WHEN** the readiness evidence run is defined
- **THEN** the direct-grid contract/producer/exact-cell/staging/negative/idempotency coverage maps to `tests/test_direct_grid_e2e.py`, `tests/test_forcing_producer.py`, and `tests/test_shud_runtime.py`
- **THEN** the DB migration coverage maps to `tests/test_migrations.py`
- **THEN** no existing test is deleted, skipped, or weakened to obtain a pass.

### Requirement: Real-object-store and real-DB smoke on node-27
The platform SHALL run a direct-grid smoke against a real object store and real database on node-27, using a dedicated synthetic direct-grid evidence contract, to confirm the pinned release behaves correctly outside in-memory fixtures without touching any production model instance or the production display.

#### Scenario: Node-27 smoke exercises real backends
- **WHEN** the readiness smoke is executed on node-27
- **THEN** it reads canonical evidence-contract inputs from the real filesystem-backed evidence-package path on node-27 (`openspec/changes/cmfd-direct-grid-platform-readiness/evidence/synthetic-package/` under the deployed checkout — the real disk the deployment reads canonical evidence inputs from; NOT `/home/ghdc/nwm/object-store/{forcing,models}` which are production forcing-time-series roots read-only to the smoke's user role and out-of-scope for evidence writes)
- **THEN** the derived rows the smoke depends on are present in the real database (either written by the same `§2.4` smoke run, OR written by the paired `§2.3` evidence-only registration transaction with `§2.4` attesting a byte-identical read-back roundtrip and confinement to the dedicated non-production identity)
- **THEN** it confirms direct-grid production does not fall back to legacy IDW station loading (structural argument citing scheduler-dispatch filter + producer station-loader filter + runtime path selection is acceptable when the smoke intentionally does not execute a live pipeline pass against the evidence identity)
- **THEN** it records the live database's applied schema migration version and confirms it equals the manifest's pinned `db_schema_migration_version`
- **THEN** the smoke evidence records the node-27 host, the pinned `baseline_commit`, and the manifest checksum; when the code carrier commit at run time differs from the pinned `baseline_commit` (because an evidence carrier landed after manifest freeze), the smoke evidence additionally records the `code_carrier_sha` and asserts empty tree-diff between the two commits on all manifest-identity paths (`workers/`, `apps/`, `services/`, `packages/`, `db/`, `schemas/`).

#### Scenario: Smoke runs against the synthetic evidence contract with isolation and cleanup
- **WHEN** the node-27 smoke's `§2.3`+`§2.4` pair provisions its direct-grid contract and writes derived rows to the real database (whether directly in the `§2.4` smoke or via the paired `§2.3` evidence-only registration transaction that the `§2.4` smoke attests read-back for)
- **THEN** it uses the hand-assembled synthetic direct-grid evidence contract registered as a dedicated evidence-only `core.model_instance` row carrying `resource_profile.direct_grid_forcing`, with construction provenance and SHA-256 checksums recorded in the evidence
- **THEN** it does not read its contract from, or write derived rows against, any of the 13 live production model instances
- **THEN** derived rows are confined to a dedicated non-production identity (dedicated `basin_version_id`/`model_id`) and any `met.met_station` mirror rows carry `active_flag=false`, so the station-MVT layer cannot display mixed old/new stations
- **THEN** after evidence capture the smoke's derived and mirror rows are removed, or verifiably remain confined to the inactive dedicated identity, and a display spot-check confirms production display is unaffected.

### Requirement: Minimal-basin execution with the production SHUD binary
The platform SHALL execute a minimal basin end-to-end with the production SHUD binary, staging the hand-assembled synthetic multi-station direct-grid evidence package, to confirm the pinned solver stages and runs a standard multi-station direct-grid package.

#### Scenario: Minimal basin runs on the production binary
- **WHEN** the minimal-basin readiness execution runs
- **THEN** it uses the production SHUD binary (`shud`) identified in the readiness manifest
- **THEN** it stages the synthetic minimal multi-station direct-grid evidence package — rewritten-`FORC` `.sp.att`, binding manifest, standard multi-station `.tsd.forc`, and per-station CSVs — whose construction provenance and SHA-256 checksums are recorded in the evidence; the package is hand-assembled, not produced by a mapping builder, and no production basin package is rewritten
- **THEN** it stages a standard multi-station direct-grid forcing package and does not rewrite `.sp.att` to a single station
- **THEN** staged `.sp.att FORC` values are within the staged `.tsd.forc` `ID` set
- **THEN** the execution evidence records the executed `shud` binary path used on node-22 (production binary, no rebuild), as identified in the readiness manifest
- **THEN** the execution evidence records the node-22 Slurm/SHUD runtime host, the pinned `baseline_commit`, and the readiness manifest checksum.

### Requirement: G9 capacity baseline is reported against deployment config
The platform SHALL produce a Gate G9 capacity baseline that estimates direct-grid resource usage against the deployment configuration and compares it to live legacy baselines measured on node-27 before any migration.

#### Scenario: Capacity estimate uses the deployment-configured limits
- **WHEN** the G9 capacity baseline is produced
- **THEN** it estimates DB timeseries rows as `station_count × timestep_count × output_variable_count`
- **THEN** it evaluates the estimate against the producer limits of 10,000 stations, 10,000 timesteps, 10,000,000 timeseries rows, and ~32 MiB manifest
- **THEN** it evaluates the estimate against the runtime staging byte and line limits in `workers/shud_runtime/runtime.py`
- **THEN** it records the deployment configuration values actually used for the check, not only the formula
- **THEN** the report records the pinned `baseline_commit` and the readiness manifest checksum.

#### Scenario: Capacity baseline compares to live legacy facts
- **WHEN** the G9 capacity baseline is produced
- **THEN** the live legacy basin count, station count, and `met.forcing_station_timeseries` row counts are measured on node-27 against the active primary database at evidence-production time, recording the exact SQL queries and measurement timestamps
- **THEN** the prior audit figures (13 basins, 6,290 legacy stations, ~121M rows per two weeks ≈ 8M rows/day) serve only as cross-check references, not as substitutes for live measurement
- **THEN** it reports the expected direct-grid reduction (used-cell counts about 5x fewer than legacy stations)
- **THEN** exceeding any limit is treated as a blocker requiring a separate capacity change rather than a temporary relaxation
- **THEN** the report records the pinned `baseline_commit` and the readiness manifest checksum.

### Requirement: Readiness is judged on pinned-commit evidence not checkbox state
The platform SHALL judge migration readiness on pinned-commit test results, smoke evidence, and audit outcome, and SHALL NOT treat OpenSpec checkbox completion as evidence of readiness.

#### Scenario: Checkbox completion does not certify readiness
- **WHEN** OpenSpec tasks for direct-grid capabilities are marked complete
- **THEN** readiness is not certified on checkbox state alone
- **THEN** readiness certification requires the pinned manifest, passing re-run evidence, node-27 smoke, minimal-basin execution, and the G9 capacity baseline with no unresolved limit breach
- **THEN** any drift between OpenSpec task state and code state is recorded in the evidence rather than assumed absent.

### Requirement: Readiness evidence package binds all artifacts to a single baseline
The platform SHALL assemble a readiness evidence package that indexes the readiness manifest and every readiness evidence artifact, and SHALL treat the evidence set as valid only when every artifact references the identical readiness manifest checksum and the identical `baseline_commit`, and — when an artifact records a `code_carrier_sha` distinct from `baseline_commit` — MUST additionally verify that the tree-diff between the two commits on manifest-identity paths is empty.

#### Scenario: Evidence package indexes all artifacts against one baseline
- **WHEN** the readiness evidence package is assembled
- **THEN** it indexes the readiness manifest and its `.sha256` companion, the manifest completeness-check output, the suite re-run records, the node-27 smoke record, the minimal-basin execution record, and the G9 capacity baseline report
- **THEN** every indexed artifact references the same readiness manifest checksum
- **THEN** every indexed artifact references the same `baseline_commit`
- **THEN** for every indexed artifact that records a `# code_carrier_sha=<40-hex>` header line distinct from `baseline_commit`, the cross-artifact consistency check parses that line and asserts `git diff <baseline_commit>..<code_carrier_sha> -- workers/ apps/ services/ packages/ db/ schemas/` is empty; a non-empty diff invalidates the artifact
- **THEN** artifacts captured before the `code_carrier_sha` contract landed (e.g. `db-registration-2.3.node-27.pass.log`) are grandfathered by the sibling `smoke-2.4.node-27.pass.log` retro-attest sharing the same `code_carrier_sha` and manifest-identity path empty-diff.

#### Scenario: Mismatched baseline references invalidate the evidence set
- **WHEN** any indexed artifact references a readiness manifest checksum or a `baseline_commit` that differs from the rest of the evidence set, OR records a `code_carrier_sha` that produces a non-empty tree-diff on manifest-identity paths against `baseline_commit`
- **THEN** the evidence set is invalid
- **THEN** readiness certification is blocked until a consistent evidence set is produced on a single pinned baseline.
