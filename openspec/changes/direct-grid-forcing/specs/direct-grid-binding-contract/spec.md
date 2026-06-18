## ADDED Requirements

### Requirement: Direct-grid station binding contract
A direct-grid basin/model asset SHALL provide a complete binding from every SHUD forcing station to exactly one canonical grid cell.

#### Scenario: Binding contains required station fields
- **WHEN** direct-grid validation reads the basin/model asset
- **THEN** every forcing station binding includes `station_id`, `shud_forcing_index`, `forcing_filename`, `longitude`, `latitude`, `grid_id`, and `grid_cell_id`
- **THEN** the station also includes SHUD output coordinates `x`, `y`, and `z` or a documented equivalent source for those fields
- **THEN** production fails before output publish when any required station field is missing
- **THEN** no forcing package or forcing version is marked ready
- **THEN** IDW weights are not computed as a fallback for that run.

#### Scenario: Binding manifest contains required asset identity
- **WHEN** direct-grid validation reads the basin/model asset manifest
- **THEN** the manifest includes `binding_uri`, `binding_checksum`, `model_input_package_id`, `.sp.att` path and checksum, `applicable_source_ids`, `grid_id`, and `grid_signature`
- **THEN** these identities are used to validate that station bindings belong to the staged model input package
- **THEN** production fails before output publish when any required manifest identity is missing
- **THEN** no forcing package or forcing version is marked ready
- **THEN** IDW weights are not computed as a fallback for that run.

#### Scenario: Binding checksum mismatch blocks readiness
- **WHEN** the binding JSON loaded from `binding_uri` does not match the manifest `binding_checksum`
- **THEN** direct-grid validation fails before value extraction or output publish
- **THEN** no forcing package or forcing version is marked ready
- **THEN** IDW weights are not computed as a fallback for that run
- **THEN** failure details include expected and actual binding checksums.

#### Scenario: Forcing indexes are contiguous
- **WHEN** direct-grid validation reads station bindings
- **THEN** `shud_forcing_index` values are unique and contiguous from 1 to station count
- **THEN** `forcing_filename` values are unique and safe for SHUD package output
- **THEN** production fails before output publish when indexes or filenames violate the contract
- **THEN** no forcing package or forcing version is marked ready
- **THEN** IDW weights are not computed as a fallback for that run.

#### Scenario: Station binding targets one grid cell
- **WHEN** direct-grid validation reads a station binding
- **THEN** the binding maps that station to exactly one `grid_cell_id`
- **THEN** runtime direct-grid production treats that grid cell as weight 1.0 for every output variable.

### Requirement: Direct-grid contract is loaded through an authoritative repository entrypoint
The system SHALL expose direct-grid forcing mapping metadata through a single repository/store entrypoint backed by the selected model asset manifest.

#### Scenario: Repository reads manifest-backed direct-grid contract
- **WHEN** forcing production asks the repository for the forcing mapping contract for a `model_id` and `basin_version_id`
- **THEN** the repository returns the contract derived from the selected model asset manifest
- **THEN** the returned contract preserves `binding_uri`, `binding_checksum`, `model_input_package_id`, `.sp.att` path and checksum, `applicable_source_ids`, `grid_id`, `grid_signature`, and station `grid_cell_id` values
- **THEN** callers do not need to read direct-grid contract fields from unrelated database tables.

#### Scenario: Database mirror cannot override manifest identity
- **WHEN** database rows mirror direct-grid station or mapping metadata for query performance
- **THEN** those rows are treated only as derived cache
- **THEN** a database mirror value cannot replace or override manifest `binding_checksum`, `model_input_package_id`, `.sp.att` checksum, `applicable_source_ids`, `grid_id`, or `grid_signature`
- **THEN** a mismatch between mirror values and manifest identities is handled as a direct-grid contract validation failure before readiness.

### Requirement: Direct-grid assets declare grid identity
A direct-grid basin/model asset SHALL declare the canonical grid identity that its bindings target.

#### Scenario: Grid identity matches canonical product
- **WHEN** direct-grid production runs for a source/cycle
- **THEN** the asset `grid_id` matches the canonical product `grid_id`
- **THEN** the asset grid signature matches the canonical grid definition content signature computed from schema version plus ordered coordinate arrays or ordered cells after longitude normalization
- **THEN** production fails before output publish when grid identity does not match
- **THEN** no forcing package or forcing version is marked ready
- **THEN** IDW weights are not computed as a fallback for that run.

#### Scenario: Source scope permits current source
- **WHEN** direct-grid production runs for a source/cycle
- **THEN** the current `source_id` is present in the asset manifest `applicable_source_ids`
- **THEN** production fails before output publish when the current source is not listed
- **THEN** no forcing package or forcing version is marked ready
- **THEN** IDW weights are not computed as a fallback for that run.

#### Scenario: GFS and IFS longitude conventions are normalized
- **WHEN** station coordinates are validated for GFS or IFS direct-grid assets
- **THEN** station geometry uses a WGS84-compatible `[-180, 180)` longitude convention for SHUD output
- **THEN** any source-native longitude convention is retained only as metadata and does not replace `grid_cell_id` as the lookup key.

### Requirement: Triangle forcing ownership is validated
The system SHALL verify that the direct-grid station contract is consistent with the SHUD triangle forcing ownership in the model input package.

#### Scenario: sp.att FORC references valid station indexes
- **WHEN** direct-grid model asset validation inspects the `.sp.att` file
- **THEN** every triangle `FORC` value references an existing `shud_forcing_index`
- **THEN** no triangle references zero, negative, missing, or out-of-range forcing indexes.

#### Scenario: Station contract mismatch blocks readiness
- **WHEN** `.sp.att` `FORC` references or station count do not match direct-grid bindings
- **THEN** model asset validation fails with a direct-grid contract error
- **THEN** no forcing package or forcing version is marked ready
- **THEN** IDW weights are not computed as a fallback for that run.

#### Scenario: Model input package identity mismatch blocks readiness
- **WHEN** the staged model input package identity or `.sp.att` checksum differs from the direct-grid binding manifest identity
- **THEN** model asset validation fails with a direct-grid contract error
- **THEN** no forcing package or forcing version is marked ready
- **THEN** IDW weights are not computed as a fallback for that run.
