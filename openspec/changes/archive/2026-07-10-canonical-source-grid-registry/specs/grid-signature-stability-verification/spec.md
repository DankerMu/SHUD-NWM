## ADDED Requirements

### Requirement: Grid signature is verified stable before registration
The registry SHALL verify that a grid's `grid_signature` is invariant across cycles, required variables, and download backends before the grid enters the registry.

#### Scenario: Signature identical across cycles
- **WHEN** a grid is verified for registration using representative cycles
- **THEN** the `grid_signature` is identical across all verified cycles
- **THEN** `grid_cell_id` values are stable across those cycles
- **THEN** verification fails when the signature or any `grid_cell_id` changes between cycles.

#### Scenario: Signature identical across required variables
- **WHEN** a grid is verified across all required variables for the source
- **THEN** every required variable yields the same `grid_signature`
- **THEN** verification fails when any variable produces a different signature.

#### Scenario: Signature identical across download backends
- **WHEN** a grid is verified across the applicable download backends
- **THEN** the `grid_signature` is identical regardless of download backend
- **THEN** the bbox clip range is fixed across backends
- **THEN** verification fails when a backend change alters the signature or clip range.

### Requirement: Coordinate normalization does not change cell identity
The registry SHALL verify that latitude ordering and source-native longitude conventions normalize without changing cell identity.

#### Scenario: NetCDF latitude ascending or descending is identity-invariant
- **WHEN** a grid's NetCDF latitude axis is ascending versus descending
- **THEN** the normalized cell identity and `grid_signature` are unchanged
- **THEN** verification fails when latitude ordering changes the cell identity.

#### Scenario: GFS 0..360 and IFS -180..180 normalize per platform rule
- **WHEN** a GFS source uses `0..360` longitudes and an IFS source uses `-180..180` longitudes
- **THEN** both normalize to the `[-180, 180)` convention per the platform rule
- **THEN** the normalized cell identity is comparable across sources
- **THEN** verification fails when normalization is not applied consistently.

### Requirement: Product upgrade changes the signature
The registry SHALL require that a source product upgrade changes the `grid_signature`.

#### Scenario: Upgraded product produces a different signature
- **WHEN** a grid is produced by an upgraded source product
- **THEN** its `grid_signature` differs from the pre-upgrade signature
- **THEN** verification treats an unchanged signature after a declared product upgrade as a failure.

### Requirement: Dynamically cropped grids are refused
The registry SHALL refuse any grid whose cells are dynamically cropped per cycle.

#### Scenario: Per-cycle cropping shifts cell ids
- **WHEN** a candidate grid's `grid_cell_id`s or signature move because the canonical product is cropped by a different bbox per cycle
- **THEN** the grid is refused entry to the registry
- **THEN** the failure states that the canonical grid contract must be stabilized before registration.
