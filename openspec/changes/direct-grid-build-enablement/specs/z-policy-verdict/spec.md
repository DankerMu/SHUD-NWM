## ADDED Requirements

### Requirement: z_policy verdict is produced by a narrow three-question solver audit
The change SHALL produce a written `z_policy` verdict from a narrow audit of the pinned SHUD solver commit `3aec65755926c478e13ca7d4fea80715e4e90345` that answers exactly three questions — (a) does the `.tsd.forc` read chain read the station-row `Z` column, (b) what is that `Z` used for, and (c) what is the production `.cfg` correction-switch status — and the verdict value SHALL be one of `sentinel`, `model_dem_at_cell_center`, or `canonical_orography`. The audit SHALL NOT expand into a full solver re-audit.

#### Scenario: Audit answers the three questions and records an allowed verdict
- **WHEN** the `z_policy` verdict evidence file is produced
- **THEN** it records an answer to each of the three questions (Z read?, Z used for?, `.cfg` switch?) with line-level citations into the pinned solver source
- **THEN** it records the pinned commit `3aec65755926c478e13ca7d4fea80715e4e90345` and how the audit oracle was obtained (local `SHUD/` tree HEAD and/or the node-22 forensic command)
- **THEN** the recorded verdict value is exactly one of `sentinel`, `model_dem_at_cell_center`, or `canonical_orography`.

#### Scenario: A verdict outside the allowed set is rejected
- **WHEN** a `z_policy` value that is not one of `sentinel`, `model_dem_at_cell_center`, or `canonical_orography` is supplied to the mapping builder
- **THEN** the builder fails closed (per `ALLOWED_Z_POLICIES`) and emits no binding.

### Requirement: sentinel requires proof that station Z is unused; otherwise an explicit elevation source
The verdict SHALL select `sentinel` only when the audit proves the production SHUD solver does not use the station `Z` in any numerical computation. When the audit shows the station `Z` participates in a numerical computation, the verdict SHALL select an explicit elevation source (`model_dem_at_cell_center` or `canonical_orography`) and SHALL NOT select `sentinel`.

#### Scenario: Station Z participates in numerical computation
- **WHEN** the audit finds the station `Z` (`xyz[2]`) consumed by the temperature elevation correction `TemperatureOnElevation(t0, element_z, station_z)` at `SHUD/src/ModelData/MD_ET.cpp:32`
- **THEN** the verdict is an explicit elevation source, not `sentinel`
- **THEN** the recorded verdict is `model_dem_at_cell_center` (the explicit source available from the model package DEM at the cell center), with `canonical_orography` documented as the preferred upgrade pending a registry orography column.

#### Scenario: sentinel chosen without proof that Z is unused is rejected
- **WHEN** a `sentinel` verdict is asserted but the audit has not proven the station `Z` is unused by the solver
- **THEN** the verdict is rejected as non-conforming (per docs §7.5 / §P0.3), because the solver's `NA_VALUE` (`-9999`) short-circuit disables a correction the solver otherwise applies rather than proving `Z` is unused.

### Requirement: The verdict evidence file is the single z_policy authority for the mapping builder
The committed `z_policy` verdict evidence file SHALL be the single source of truth for the mapping builder's `z_policy` input. The builder SHALL take its `ZPolicy.policy_name` and its provenance checksum from that verdict — resolved and verified through the pinned mechanism in §"The verdict is resolved from a pinned location and a pinned checksum", never from an arbitrary caller-supplied file — SHALL bind the verdict provenance into the emitted binding, and SHALL NOT invent a numeric `z` default. When the verdict is an explicit elevation source, the builder SHALL provide a per-cell `z` for every used cell, derived per §"per_cell_z derivation for model_dem_at_cell_center is deterministic and total over used cells".

#### Scenario: Builder binds the verdict and its provenance
- **WHEN** the mapping builder emits a binding under this change
- **THEN** the `ZPolicy.policy_name` equals the value recorded in the verdict evidence file
- **THEN** the verdict evidence file checksum is bound into the emitted binding's `z_policy` provenance (carried through the existing `readiness_manifest_checksum` provenance slot).

#### Scenario: Missing verdict or missing provenance fails closed
- **WHEN** the builder is invoked with no verdict evidence file, or with a `z_policy` whose provenance checksum is blank
- **THEN** the builder fails closed and writes no binding output.

#### Scenario: Explicit-source verdict with an uncovered used cell fails closed
- **WHEN** the verdict is `model_dem_at_cell_center` or `canonical_orography` and a used cell has no per-cell `z` value
- **THEN** the builder fails closed (`ZPolicyCellMissingError`) and never substitutes a numeric default.

### Requirement: The verdict is resolved from a pinned location and a pinned checksum
Verdict resolution SHALL live in a dedicated new module `workers/mapping_builder/z_policy_verdict.py` (no existing library stage is modified) that pins, as code constants, (a) the expected verdict value `model_dem_at_cell_center` and (b) the SHA-256 of the committed verdict evidence file. The default resolution path SHALL be the committed evidence file: `openspec/changes/direct-grid-build-enablement/evidence/z-policy-solver-audit-verdict.md` while this change is active, and — because archiving relocates the whole change directory — the same file under the archive path pattern `openspec/changes/archive/<archive-date>-direct-grid-build-enablement/evidence/z-policy-solver-audit-verdict.md` after archive. The pinned checksum, not the path, is the authority anchor: any explicit path override (e.g. `--z-policy-verdict-path`) SHALL be recorded in the evidence package, and the file resolved from any path SHALL hash to the pinned SHA-256 and record the pinned verdict value — otherwise the builder SHALL fail closed with no binding output. The verified checksum SHALL be the value bound into `ZPolicy.readiness_manifest_checksum`.

#### Scenario: Default resolution verifies against the pinned authority
- **WHEN** the builder resolves the verdict from the default committed location (the active-change path, or its archive relocation after archive)
- **THEN** the file's SHA-256 equals the pinned constant and the parsed verdict value equals the pinned `model_dem_at_cell_center`
- **THEN** that verified checksum is bound into `ZPolicy.readiness_manifest_checksum`.

#### Scenario: A verdict file that does not match the pinned authority fails closed
- **WHEN** the resolved verdict file (whether from the default path or an override) has a SHA-256 that does not equal the pinned constant, or records a verdict value different from the pinned expected value
- **THEN** the builder fails closed and writes no binding output — an arbitrary substitute file cannot satisfy the single-authority requirement.

#### Scenario: A path override is explicit and evidence-recorded
- **WHEN** an explicit verdict-path override is supplied and the file at that path hashes to the pinned SHA-256
- **THEN** the build proceeds and the override path is recorded in the evidence package.

### Requirement: per_cell_z derivation for model_dem_at_cell_center is deterministic and total over used cells
For the `model_dem_at_cell_center` verdict the builder SHALL derive `per_cell_z` with the pinned sampler `nearest_mesh_node_elevation_v1`, implemented in `workers/mapping_builder/z_policy_verdict.py`, with the sampler rule identifier recorded in the evidence package:
- **DEM source:** the node-table `Elevation` column of the baseline `.sp.mesh` (node rows `ID X Y AqDepth Elevation`), read under the same G0-recorded package checksum as every other baseline asset — the same mesh-node elevations from which the solver derives element `z_surf`.
- **CRS handling:** the registered WGS84 cell center is transformed into the package CRS via the package's checksum-bound `gis/*.prj` (per-package `PROJCS["unknown"]`; no global CRS assumption) — the same transform basis that produces station `x`/`y`.
- **Sampling rule:** `z` equals the `Elevation` of the mesh node minimizing planar Euclidean distance to the transformed cell center in the package CRS; a distance tie SHALL break deterministically to the smallest node `ID`.
- **Outside-hull rule:** nearest-node sampling requires no mesh containment, so a used cell whose registered center lies outside the triangulated mesh hull (routine for boundary cells under nearest-cell ownership; guaranteed in small basins such as the keliya fixture) SHALL still sample the nearest node's `Elevation` — never a numeric default and never a silent skip. The sampler SHALL be total over used cells; a missing `per_cell_z` entry at binding time still fails closed (`ZPolicyCellMissingError`).

#### Scenario: Cell-center z equals the nearest mesh node elevation (concrete keliya oracle)
- **WHEN** the sampler derives `z` for a designated used cell of the keliya fixture
- **THEN** the sampled `z` equals the `Elevation` of the `keliya.sp.mesh` node nearest to that cell center transformed through `gis/keliya.prj`, recomputed independently in the test and pinned there as a literal expected value.

#### Scenario: A used cell center outside the mesh hull still samples deterministically
- **WHEN** a used cell's registered center lies outside the mesh hull (at least one such cell is exercised in the keliya fixture)
- **THEN** the sampler returns the nearest node's `Elevation` under the same rule — never a numeric default, never fail-open — and the result is identical across runs.

#### Scenario: Distance ties break to the smallest node ID
- **WHEN** two mesh nodes are equidistant from a transformed cell center
- **THEN** the sampler selects the node with the smallest `ID`, keeping the derivation deterministic.
