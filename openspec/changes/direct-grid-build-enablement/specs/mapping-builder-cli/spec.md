## ADDED Requirements

### Requirement: The CLI chains the G0–G5 stages end to end and fails closed
`workers/mapping_builder/cli.py` SHALL run the existing library stages in order — G0/G1 integrity, then G2 + ownership algorithm, then `.sp.att` `FORC` rewrite (G4), then binding + manifest emission (G5, consuming the `z_policy` verdict), then evidence-package assembly — and SHALL write the variant package only after every gate passes. On any gate blocker it SHALL write no partial output. The CLI SHALL chain the existing stages and SHALL NOT re-implement stage logic.

#### Scenario: End-to-end build over a valid package
- **WHEN** the CLI is invoked on a valid release-frozen baseline package and a registered grid snapshot
- **THEN** it runs G0/G1 → G2 + ownership → G4 rewrite → G5 binding → evidence in that order
- **THEN** it writes the variant package containing the rewritten `.sp.att`, the direct-grid binding, the manifest, and the evidence package
- **THEN** the emitted manifest (embedding `station_bindings`) parses cleanly through the existing parser entry `workers/forcing_producer/direct_grid_contract.py::load_forcing_mapping_contract_from_manifest`
- **THEN** the standalone binding artifact referenced by `binding_uri` matches the manifest per the `direct-grid-binding-artifact` G5 cross-consistency requirement (the manifest's `binding_checksum` equals the SHA-256 of the binding artifact bytes, and the manifest's `station_bindings` row set equals the binding artifact's row set element-for-element).

#### Scenario: A gate blocker produces no output
- **WHEN** any stage raises a gate blocker (e.g. a G0 checksum mismatch or a G5 contract mismatch)
- **THEN** the CLI fails closed and writes no variant package, binding, manifest, or evidence.

### Requirement: Input authority is the object-store release-frozen package
The CLI SHALL resolve the baseline model package only from the object-store release-frozen shape `<object-store-root>/models/basins_<basin>_shud/<release>/package/`, where the object-store root defaults to `/home/ghdc/nwm/object-store` and MAY be changed only via an explicit `--object-store-root` option; any non-default root SHALL be recorded in the evidence package (the same recording discipline as the dev-workspace override) — this is the sanctioned channel for test runs (e.g. the keliya fixture staged under a tmp root). The CLI SHALL reject any `Basins` dev-workspace path unless an explicit `--allow-dev-workspace` operator flag is supplied, in which case the override SHALL be recorded in evidence with a rationale. A path that is neither object-store-shaped under the configured root nor a recognized `Basins` dev-workspace path SHALL fail closed.

#### Scenario: Object-store release path is accepted
- **WHEN** the CLI input path is an object-store release-frozen package path under the configured root
- **THEN** the CLI proceeds with the build.

#### Scenario: Overridden object-store root is accepted and recorded
- **WHEN** the CLI is invoked with `--object-store-root` pointing at a non-default root (e.g. a tmp root where the keliya fixture is staged as `models/basins_keliya_shud/<release>/package/`) and the input path is object-store-shaped under that root
- **THEN** the CLI proceeds with the build and records the non-default root in the evidence package.

#### Scenario: Dev-workspace path is rejected without the override flag
- **WHEN** the CLI input path is a `Basins` dev-workspace path (node-27 `/home/ghdc/nwm/Basins/...` or node-22 `/volume/nwm/Basins/...`) and no override flag is set
- **THEN** the CLI fails closed and does not read the package or write any output.

#### Scenario: Dev-workspace override is explicit and recorded
- **WHEN** the CLI input path is a dev-workspace path and the explicit override flag is set
- **THEN** the CLI proceeds but records the override path and rationale in the evidence package.

#### Scenario: A path that is neither object-store-shaped nor dev-workspace fails closed
- **WHEN** the CLI input path is neither shaped `<object-store-root>/models/basins_<basin>_shud/<release>/package/` under the configured root nor a recognized `Basins` dev-workspace path
- **THEN** the CLI fails closed and does not read the package or write any output.

### Requirement: The CLI produces only offline assets with zero production writes
On the actual CLI invocation path the builder SHALL NOT emit any runtime-producer artifact (§8.1): no cycle-dated `.tsd.forc`, no per-station weather CSVs, and no `met.interp_weight` / `met.met_station` / `met.forcing_version` database rows or cycle lineage. The CLI SHALL open baseline files read-only and SHALL write only into the new variant package tree.

#### Scenario: Forbidden runtime outputs are never produced on the CLI path
- **WHEN** the CLI completes a build
- **THEN** the written variant tree contains no cycle-dated `.tsd.forc`, no per-station weather CSVs, and no legacy CMFD weather CSVs in the active forcing tree
- **THEN** no `met.*` database rows are written.

#### Scenario: A forbidden artifact in the written tree fails closed
- **WHEN** a forbidden runtime artifact would appear in the written variant tree
- **THEN** the CLI fails closed as a §8.1 blocker and leaves no variant package.

### Requirement: The CLI build is deterministic
Given the same release-frozen baseline package, the same registered grid snapshot, and the same algorithm version, the CLI SHALL produce byte-identical binding, `.sp.att`, manifest, and evidence outputs — strict raw-byte identity with no field masking. To keep the evidence bytes deterministic the CLI SHALL NOT populate the library's checksum-excluded `build_timestamp` evidence field (`EVIDENCE_CHECKSUM_EXCLUDED_FIELDS` in `workers/mapping_builder/evidence.py`): it SHALL remain unset (`None`) on the CLI path, and no wall-clock value SHALL enter any emitted artifact byte. Human-readable timing belongs in logs/stdout only.

#### Scenario: Two runs are byte-identical
- **WHEN** the CLI is run twice on identical inputs
- **THEN** the emitted binding, `.sp.att`, manifest, and evidence bytes are byte-identical across the two runs, compared raw with no field masking
- **THEN** the emitted evidence package records `build_timestamp` as unset.
