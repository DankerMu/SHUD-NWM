## ADDED Requirements

### Requirement: Baseline package integrity is verified before mapping
The mapping builder SHALL verify baseline package and geometry integrity (Gates G0 and G1) and fail closed before any mapping work when integrity is not satisfied.

#### Scenario: Baseline checksum and parseability are verified
- **WHEN** the builder loads a baseline basin model package
- **THEN** the baseline package checksum is recomputed and recorded
- **THEN** the `.sp.mesh` and `.sp.att` files are parsed successfully
- **THEN** the builder fails closed and writes no mapping output when the checksum or parse fails.

#### Scenario: Element IDs are unique, contiguous, and consistent across mesh and att
- **WHEN** the builder reads element identifiers from the `.sp.att` `INDEX` column and the `.sp.mesh` `ID` column
- **THEN** element IDs are unique and contiguous from 1
- **THEN** the `.sp.mesh` element-ID set equals the `.sp.att` element-ID set
- **THEN** the element counts are equal
- **THEN** the builder fails closed when any of these conditions is violated.

#### Scenario: Element triangles are non-degenerate (G1 geometry validity)
- **WHEN** the builder reads each element's three vertex IDs from `.sp.mesh` and the corresponding node X/Y coordinates in the package CRS
- **THEN** the three vertex IDs of each element are pairwise distinct and each references an existing mesh node
- **THEN** the triangle formed by the three node X/Y coordinates is non-degenerate, meaning its unsigned planar area in the package CRS is strictly greater than a declared numeric tolerance (three collinear vertices producing zero-area triangles are rejected)
- **THEN** the builder fails closed as a G1 blocker with no mapping output when any element has a repeated vertex ID, a vertex ID missing from the mesh node set, or three vertices whose triangle area is at or below the tolerance.

#### Scenario: Old FORC values and legacy tsd.forc references are legal
- **WHEN** the builder inspects the baseline `.sp.att` `FORC` column
- **THEN** every old `FORC` value is a positive integer
- **THEN** if a baseline `.tsd.forc` is present, its referenced forcing IDs are legal
- **THEN** the builder fails closed when any old `FORC` value is non-positive or non-integer.

#### Scenario: Ancillary dependency inventory is complete
- **WHEN** the builder scans the baseline package for ancillary `*.tsd.*` inputs
- **THEN** the dependency inventory of all ancillary `*.tsd.*` files is recorded and complete
- **THEN** the builder fails closed when an ancillary dependency cannot be inventoried.

#### Scenario: Model CRS is read only from the package prj
- **WHEN** the builder determines the model CRS
- **THEN** the CRS is read only from the package `gis/*.prj` and is checksum-bound in the evidence
- **THEN** the builder makes no global CRS assumption because `.sp.mesh` carries no CRS metadata and live packages are `PROJCS["unknown"]` custom Albers (×12) or Transverse Mercator (qhh) with no EPSG code
- **THEN** the builder fails closed when the package `.prj` is missing, unparseable, or not convertible with WGS84.

#### Scenario: domain.shp is not an algorithm input
- **WHEN** the builder performs any mapping computation
- **THEN** `domain.shp` is used only for visualization comparison images and never as a geometry or element-ID authority
- **THEN** shapefile row order is never treated as element ID.

#### Scenario: Duplicate-coordinate and non-grid baseline stations are classified
- **WHEN** the builder inspects baseline stations
- **THEN** multiple stations at identical coordinates are explicitly registered (live: zhaochen_mc has 4 stations at identical coordinates with Z=-9999)
- **THEN** non-grid baselines are classified rather than assumed to be CMFD grid points (live: zhaochen_wem is 5 irregular points, filenames X1..X5.csv, 0.02° spacing)
- **THEN** startdate heterogeneity is recorded (live: baseline `.tsd.forc` startdates span 1951–2024 across the 13 basins).

#### Scenario: Baseline is never modified
- **WHEN** the builder performs any integrity or classification step
- **THEN** the baseline package, `.sp.att`, `.tsd.forc`, and historical forcing versions are opened read-only and left unchanged (INV-1)
- **THEN** the builder records but never repairs known-harmless baseline deviations such as build-machine absolute paths in `.tsd.forc` line 2.
