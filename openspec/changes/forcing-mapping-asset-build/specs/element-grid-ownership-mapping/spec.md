## ADDED Requirements

### Requirement: Grid identity is verified against the registered snapshot (G2)
The mapping builder SHALL verify grid identity as a precondition to any element-cell mapping (Gate G2, docs §Gate G2 and design.md D5.G2). Verification MUST use the registered grid snapshot loaded from `canonical-source-grid-registry`, MUST recompute the snapshot's `grid_signature` via the shared helper, and MUST fail closed before writing any output when any G2 condition is violated.

#### Scenario: Grid snapshot is loaded from the registry by (source_id, grid_id)
- **WHEN** the builder starts element-cell mapping
- **THEN** the builder looks up the registered grid snapshot from `canonical-source-grid-registry` by `(source_id, grid_id)`
- **THEN** the builder fails closed as a G2 blocker when no matching snapshot is registered, with no output written.

#### Scenario: grid_signature is recomputed via the shared helper and matches the registered value
- **WHEN** the builder loads the registered grid snapshot
- **THEN** the builder recomputes `grid_signature` from the loaded snapshot's ordered cells using the shared helper `packages/common/grid_signature.grid_signature_hash` (per design.md §Risks and D5.G2)
- **THEN** the recomputed signature equals the snapshot's stored `grid_signature`
- **THEN** the builder never hand-rolls a signature rule
- **THEN** the builder fails closed as a G2 blocker on any mismatch, with no output written.

#### Scenario: Basin lies fully inside the registered grid coverage
- **WHEN** the builder computes element barycenters in WGS84 for mapping
- **THEN** every element barycenter lies inside the snapshot's registered coverage bbox
- **THEN** the builder never silently drops or dynamically crops uncovered elements
- **THEN** the builder fails closed as a G2 blocker when any element barycenter falls outside registered grid coverage, with no output written.

### Requirement: Element ownership is computed by nearest-cell barycenter geodesic algorithm
The mapping builder SHALL compute triangle-to-grid ownership using the versioned algorithm `nearest_cell_barycenter_geodesic_v1`, whose distance definition, tie-break, index order, and coordinate precision do not change without a new version identifier.

#### Scenario: Element representative point is the mesh barycenter
- **WHEN** the builder computes the representative point of an element
- **THEN** the point is the geometric barycenter `(v1 + v2 + v3) / 3` of the element's three mesh vertices using the `.sp.mesh` node X/Y in the package CRS
- **THEN** the barycenter is transformed to WGS84 before grid matching
- **THEN** display-layer coordinates are never used as the representative point.

#### Scenario: Nearest registered cell is selected by geodesic distance
- **WHEN** the builder matches an element barycenter to a grid cell
- **THEN** the nearest registered grid cell is selected by geodesic distance
- **THEN** for regular lat/lon grids, independent lon/lat rounding is an allowed equivalent implementation only if it yields the same result and the same tie behavior as the geodesic definition
- **THEN** an undeclared planar-degree distance is never used and `grid_cell_id` is never inferred from a coordinate string.

#### Scenario: Ties are resolved by smallest canonical ordinal
- **WHEN** more than one grid cell is within the tie tolerance of an element barycenter
- **THEN** the cell with the smallest canonical ordinal is selected
- **THEN** the distance, tie status, and candidate count are recorded per element
- **THEN** the tie decision is reproducible.

#### Scenario: Distance sanity bound rejects CRS or grid errors
- **WHEN** an element barycenter lies within valid grid coverage
- **THEN** the nearest-center distance does not exceed the local half-cell-diagonal plus a numeric tolerance
- **THEN** the builder fails closed as a blocker when the distance exceeds that bound, because it indicates a CRS, clip, or grid definition error.

### Requirement: Used-cell subset and forcing indexes are derived deterministically
The mapping builder SHALL derive the binding cell set from only the cells referenced by elements and assign contiguous forcing indexes deterministically.

#### Scenario: Only referenced cells become binding cells
- **WHEN** the builder has mapped every element to a cell
- **THEN** the used-cell subset contains only cells referenced by at least one element
- **THEN** every binding cell is referenced by at least one element and there are zero unused bindings
- **THEN** one grid cell corresponds to exactly one SHUD station.

#### Scenario: shud_forcing_index is contiguous by canonical ordinal
- **WHEN** the builder assigns forcing indexes to used cells
- **THEN** `shud_forcing_index` values are assigned `1..N`, contiguous and unique, ordered by canonical ordinal
- **THEN** the assignment is reproducible and consistent with the rewritten `.sp.att FORC` and the binding station order.

### Requirement: Small basins are refused unless explicitly approved
The mapping builder SHALL refuse to build a direct-grid mapping when the used-cell count is below four, unless an explicit approval override is recorded.

#### Scenario: Fewer than four used cells refuses by default
- **WHEN** the used-cell count is less than 4 (live: zhaochen_wem = 1 cell, zhaochen_mc = 4 cells)
- **THEN** the builder refuses by default and writes no mapping output
- **THEN** the refusal is a hard blocker, not a warning.

#### Scenario: Small-basin override is recorded in evidence
- **WHEN** an explicit small-basin approval flag is supplied for a basin with fewer than four used cells
- **THEN** the builder proceeds only with that override
- **THEN** the override, including approver identity, is recorded verbatim in the evidence package as an approval.
