# mapping-evidence-package Specification

## Purpose
TBD - created by archiving change forcing-mapping-asset-build. Update Purpose after archive.
## Requirements
### Requirement: Mapping variant produces an immutable evidence package
The mapping builder SHALL produce an immutable evidence package (§14) whose checksum is bound to the mapping-asset checksum.

#### Scenario: Evidence records baseline and grid identity
- **WHEN** the builder assembles the evidence package
- **THEN** evidence records baseline identity as the baseline package, `.sp.att`, and `.sp.mesh` checksums
- **THEN** evidence records the grid snapshot reference including ordered cells, signature, and checksum
- **THEN** evidence records the mapping algorithm identifier `algorithm_id='nearest_cell_barycenter_geodesic_v1'` and the pinned `proj_crs_database_version` obtained from the `cmfd-direct-grid-platform-readiness` readiness manifest (per design.md D2 "the PROJ database version is pinned by the readiness change and recorded in evidence (P0.1)")
- **THEN** both `algorithm_id` and `proj_crs_database_version` are inputs to the evidence checksum, so any change to either invalidates the evidence checksum.

#### Scenario: Evidence records ownership and station bindings
- **WHEN** the builder assembles the evidence package
- **THEN** evidence includes the ownership table with `element_id`, old `FORC`, new `FORC`, `grid_cell_id`, and distance per element
- **THEN** evidence includes the station binding rows and the `.sp.att` asset diff.

#### Scenario: Evidence records distance QA and capacity report
- **WHEN** the builder assembles the evidence package
- **THEN** evidence includes distance QA with min, P50, P95, and max, distances normalized by cell size, tie count, and coverage-edge count
- **THEN** evidence includes a capacity report comparing station count, timestep count, timeseries-row count, and file size against configured limits (live baseline: 6,290 stations → estimated ~1,200 used cells ≈ 5× reduction).

#### Scenario: Evidence records gate results, images, approvals, and rollback
- **WHEN** the builder assembles the evidence package
- **THEN** evidence includes the old and new ownership map images
- **THEN** evidence includes the G0–G5 gate results, approvals (including any small-basin override), and the rollback target.

#### Scenario: Evidence is immutable and checksum-bound
- **WHEN** the builder finalizes the evidence package
- **THEN** the evidence package is immutable
- **THEN** the evidence checksum is bound to the mapping-asset checksum so neither can be altered without invalidating the other.

### Requirement: Builder output is deterministic per algorithm version
The mapping builder SHALL be deterministic per algorithm version. Given the same baseline package, the same registered grid snapshot, and the same algorithm version identifier, the emitted binding, `.sp.att`, and evidence bundle SHALL be byte-identical across independent builds. Any field explicitly excluded from checksums (for example a timestamp) SHALL NOT enter any checksum and SHALL be enumerated as excluded in the evidence.

#### Scenario: Same inputs yield byte-identical binding, .sp.att, and evidence
- **WHEN** the builder is invoked twice with the same baseline package, the same registered grid snapshot, and the same algorithm version
- **THEN** the two emitted binding artifacts are byte-identical
- **THEN** the two emitted variant `.sp.att` files are byte-identical
- **THEN** the two emitted evidence bundles are byte-identical (modulo explicitly-excluded, checksum-excluded fields)
- **THEN** the two evidence checksums are equal.

#### Scenario: Excluded fields never enter any checksum and are enumerated in evidence
- **WHEN** the builder writes any field that is explicitly excluded from determinism (e.g. a build timestamp)
- **THEN** that field is enumerated in the evidence under an explicit `checksum_excluded_fields` list
- **THEN** no such field enters the binding checksum, the `.sp.att` checksum, or the evidence checksum
- **THEN** mutating an excluded field never changes any checksum.

