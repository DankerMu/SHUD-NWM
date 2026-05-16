## ADDED Requirements

### Requirement: Basins inventory imports into model registry

The system SHALL import validated Basins inventory records into the existing model registry tables using deterministic IDs and idempotent writes.

#### Scenario: Import creates registry version chain

- **WHEN** a valid Basins inventory record is imported
- **THEN** the system creates or reuses `core.basin`, `core.basin_version`, `core.river_network_version`, `core.mesh_version`, and `core.model_instance` rows linked by consistent IDs

#### Scenario: Import is idempotent for unchanged assets

- **WHEN** the same inventory and package checksum are imported more than once
- **THEN** the second import does not duplicate rows or change active model selection

#### Scenario: Changed package checksum is not silently overwritten

- **WHEN** an import targets an existing model/version with a different package checksum
- **THEN** the import fails with a structured conflict error or requires an explicit new version ID

#### Scenario: Deterministic IDs preserve source path identity

- **WHEN** inventory includes nested or aliased inputs such as `zhaochen/WEM/input/WEM` or `kashigeer/input/ksge`
- **THEN** generated IDs normalize source path components to lower-case stable identifiers while preserving original path and `shud_input_name` in metadata

### Requirement: Basin domain geometry is imported from GIS sidecars

The system SHALL import basin version geometry from `input_dir/gis/domain.{shp,shx,dbf,prj}` and SHALL fail when required shapefile sidecars are missing.

#### Scenario: Domain geometry becomes basin version geometry

- **WHEN** a Basins model has complete `domain` shapefile sidecars
- **THEN** `core.basin_version.geom` stores non-empty PostGIS geometry with the expected SRID and source URI/checksum metadata

#### Scenario: Missing domain sidecar fails import

- **WHEN** `domain.shp`, `domain.shx`, `domain.dbf`, or `domain.prj` is missing for a model selected for registry import
- **THEN** the import fails before writing partial registry rows and reports the missing sidecar file

### Requirement: River segments are imported with geometry and topology metadata

The system SHALL import river segment records from Basins GIS and SHUD river files with geometry, segment order, downstream segment ID where available, length, and source properties.

#### Scenario: Segment count matches source evidence

- **WHEN** `gis/river.shp` or `gis/seg.shp` and SHUD `.sp.riv` files are available
- **THEN** `core.river_network_version.segment_count` matches the imported `core.river_segment` row count or the import fails with a structured mismatch error

#### Scenario: Topology metadata is persisted

- **WHEN** `.sp.riv`, `.sp.rivseg`, and GIS attributes contain segment order, downstream segment, length, or source identifiers
- **THEN** imported `core.river_segment` rows preserve available topology fields and source properties; unavailable downstream IDs are stored as null with an explanatory property

#### Scenario: River segment map query can read imported geometry

- **WHEN** the imported basin version is queried through `GET /api/v1/basin-versions/{basin_version_id}/river-segments`
- **THEN** the response contains GeoJSON features with `river_segment_id`, `river_network_version_id`, `basin_version_id`, and geometry for map rendering

### Requirement: Imported models remain inactive until explicitly activated

The system SHALL default imported Basins models to inactive unless an explicit activation command or payload is supplied.

#### Scenario: Safe default import

- **WHEN** Basins registry import completes without an activation flag
- **THEN** all imported `core.model_instance.active_flag` values are false and existing active models remain unchanged

#### Scenario: Explicit activation is audited

- **WHEN** an operator activates a Basins-backed model after import
- **THEN** the active switch uses `PUT /api/v1/models/{model_id}/active`
- **AND** the system records durable audit evidence for the successful state transition, preferably in `ops.audit_log` when that table is available
- **AND** duplicate activation conflicts do not create additional audit evidence

#### Scenario: Inactive import is not used for default forecast staging

- **WHEN** a Basins model has been imported but not explicitly activated
- **THEN** active-model discovery and default forecast staging do not select that model
