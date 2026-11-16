## ADDED Requirements

### Requirement: Per-asset forcing mapping mode resolution
The system SHALL resolve the forcing mapping mode for each forcing production run from the selected basin/model asset contract.

#### Scenario: Missing mode preserves legacy IDW
- **WHEN** forcing production starts for a model asset that does not declare `forcing_mapping_mode`
- **THEN** the producer uses `idw` mode
- **THEN** existing IDW station loading, weight computation, and SHUD output behavior remain unchanged.

#### Scenario: Explicit IDW mode preserves legacy IDW
- **WHEN** forcing production starts for a model asset whose manifest declares `forcing_mapping_mode="idw"`
- **THEN** the producer uses the existing IDW path
- **THEN** direct-grid station binding validation is not required for that run.

#### Scenario: Explicit direct-grid mode is selected
- **WHEN** forcing production starts for a model asset whose manifest declares `forcing_mapping_mode="direct_grid"`
- **THEN** the producer selects the direct-grid path for that model/source/cycle
- **THEN** the producer performs direct-grid contract validation before writing ready forcing outputs.

#### Scenario: Unsupported mode fails closed
- **WHEN** forcing production starts for a model asset whose manifest declares an unsupported `forcing_mapping_mode`
- **THEN** the producer fails with an invalid mapping mode error
- **THEN** no forcing package is marked ready and no silent fallback to IDW occurs.

### Requirement: Direct-grid mode does not silently fallback
The system SHALL NOT fallback from `direct_grid` to `idw` after direct-grid mode has been explicitly selected.

#### Scenario: Direct-grid metadata is incomplete
- **WHEN** a model asset declares `forcing_mapping_mode="direct_grid"` but required direct-grid station binding metadata is missing
- **THEN** forcing production fails before value extraction or output publish
- **THEN** no forcing package or forcing version is marked ready
- **THEN** IDW weights are not computed as a fallback for that run
- **THEN** failure details identify the missing metadata.

#### Scenario: Direct-grid grid signature mismatch
- **WHEN** a model asset declares `forcing_mapping_mode="direct_grid"` and its declared grid signature differs from the canonical product grid signature
- **THEN** forcing production fails before value extraction or output publish
- **THEN** no forcing package or forcing version is marked ready
- **THEN** IDW weights are not computed as a fallback for that run
- **THEN** the failure details include expected and actual grid identity.

#### Scenario: Direct-grid model input identity mismatch
- **WHEN** a model asset declares `forcing_mapping_mode="direct_grid"` but the binding identity, model input package identity, or `.sp.att` checksum does not match the staged model package
- **THEN** forcing production fails before value extraction or output publish
- **THEN** no forcing package or forcing version is marked ready
- **THEN** IDW weights are not computed as a fallback for that run
- **THEN** failure details include expected and actual asset identities.

### Requirement: Mapping mode is recorded in lineage
Every forcing version SHALL record the mapping mode used to produce station values.

#### Scenario: Ready direct-grid forcing records lineage
- **WHEN** direct-grid forcing production completes successfully
- **THEN** the `met.forcing_version` lineage includes `forcing_mapping_mode="direct_grid"`
- **THEN** lineage identifies the binding asset or manifest section used for station-to-grid-cell mapping.

#### Scenario: Ready IDW forcing records lineage
- **WHEN** IDW forcing production completes successfully
- **THEN** the `met.forcing_version` lineage includes `forcing_mapping_mode="idw"`
- **THEN** lineage continues to identify the interpolation weight method and grid signature.
