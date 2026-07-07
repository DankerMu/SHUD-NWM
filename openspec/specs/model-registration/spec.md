# model-registration Specification

## Purpose
TBD - created by archiving change m1-gfs-forecast-loop. Update Purpose after archive.
## Requirements
### Requirement: Basin and basin version registration

The system SHALL allow registration of a basin (`core.basin`) and one or more basin versions (`core.basin_version`). A basin_version MUST reference an existing basin_id. The `basin_version_id` MUST follow the convention defined in `docs/appendices/A_id_and_versioning_convention.md` (e.g., `{basin_id}_v{N}`). The `geom` column MUST be a valid `geometry(MultiPolygon, 4490)`.

#### Scenario: Register a new basin and basin version via API

- **WHEN** a client sends `POST /api/v1/models` with a payload containing `basin_id`, `basin_name`, and `basin_version` fields including a valid MultiPolygon geometry in EPSG:4490
- **THEN** a row MUST be inserted into `core.basin` with the given `basin_id` and `basin_name`
- **THEN** a row MUST be inserted into `core.basin_version` with a `basin_version_id` following the ID convention, referencing the basin, and storing the geometry
- **THEN** the response MUST return HTTP 201 with the created `basin_version_id`

#### Scenario: Reject duplicate basin_id

- **WHEN** a client sends `POST /api/v1/models` with a `basin_id` that already exists in `core.basin`
- **THEN** the system MUST return HTTP 409 Conflict
- **THEN** no duplicate row MUST be inserted into `core.basin`

### Requirement: River network version and river segment registration

The system SHALL allow registration of a `core.river_network_version` linked to an existing `basin_version_id`. The `river_network_version_id` MUST follow the ID convention. Each river network version MUST include `segment_count` and associated `core.river_segment` rows. Each river segment MUST have `segment_order`, `downstream_segment_id`, `length_m`, `geom` (geometry(LineString, 4490)), and `properties_json`.

#### Scenario: Register a river network version with segments

- **WHEN** a client sends `POST /api/v1/river-networks` with `basin_version_id`, `version_label`, `segment_count`, and an array of segment definitions each containing `river_segment_id`, `segment_order`, `downstream_segment_id`, `length_m`, `geom` (LineString 4490), and `properties_json`
- **THEN** a row MUST be inserted into `core.river_network_version` with the given `river_network_version_id`, `basin_version_id`, `version_label`, `segment_count`, `source_uri`, and `checksum`
- **THEN** rows MUST be inserted into `core.river_segment` with composite PK `(river_segment_id, river_network_version_id)` for each segment
- **THEN** the `segment_count` MUST equal the number of segment rows inserted
- **THEN** the response MUST return HTTP 201 with the created `river_network_version_id`

#### Scenario: Reject river network version with non-existent basin_version

- **WHEN** a client sends `POST /api/v1/river-networks` with a `basin_version_id` that does not exist in `core.basin_version`
- **THEN** the system MUST return HTTP 422 with an error message indicating the basin_version does not exist

### Requirement: Mesh version registration

The system SHALL allow registration of a `core.mesh_version` linked to a `basin_version_id`. The `mesh_version_id` MUST follow the ID convention.

#### Scenario: Register a mesh version

- **WHEN** a client sends `POST /api/v1/mesh-versions` with `basin_version_id` and mesh metadata
- **THEN** a row MUST be inserted into the mesh version table with the given `mesh_version_id`
- **THEN** the response MUST return HTTP 201 with the created `mesh_version_id`

### Requirement: Model instance registration with basin version and SHUD version linkage

A `core.model_instance` MUST be created by linking a `basin_version_id`, a `river_network_version_id`, a `mesh_version_id`, a `calibration_version_id`, SHUD engine version strings, a `model_package_uri`, and a `resource_profile` (JSONB). The `model_id` MUST follow the ID convention. The referenced `basin_version`, `river_network_version`, and `mesh_version` MUST already exist in the database. The `active_flag` column (BOOLEAN) MUST default to `false`.

#### Scenario: Register a model instance with valid references

- **WHEN** a client sends `POST /api/v1/models` with `basin_version_id`, `river_network_version_id`, `mesh_version_id`, `calibration_version_id`, `shud_code_version`, `rshud_code_version`, `autoshud_code_version`, `model_package_uri`, `container_image`, and `resource_profile`
- **THEN** a row MUST be inserted into `core.model_instance` with `model_id` following the ID convention
- **THEN** column `basin_version_id` MUST reference the given `core.basin_version` record
- **THEN** column `river_network_version_id` MUST reference the given `core.river_network_version` record
- **THEN** column `mesh_version_id` MUST store the given mesh version identifier
- **THEN** column `calibration_version_id` MUST store the given calibration version identifier
- **THEN** column `shud_code_version` MUST store the exact SHUD engine version string (e.g., `shud-2.0`)
- **THEN** column `rshud_code_version` MUST store the rSHUD code version string
- **THEN** column `autoshud_code_version` MUST store the autoSHUD code version string
- **THEN** column `model_package_uri` MUST store the URI pointing to the model package in object storage
- **THEN** column `container_image` MUST store the container image reference (if provided)
- **THEN** column `resource_profile` MUST store the provided JSONB (defaulting to `'{}'` if omitted)
- **THEN** column `active_flag` MUST default to `false`

#### Scenario: Reject model instance with non-existent basin_version

- **WHEN** a client sends `POST /api/v1/models` with a `basin_version_id` that does not exist in `core.basin_version`
- **THEN** the system MUST return HTTP 422 with an error message indicating the basin_version does not exist
- **THEN** no row MUST be inserted into `core.model_instance`

#### Scenario: Reject model instance without model_package_uri

- **WHEN** a client sends `POST /api/v1/models` without a `model_package_uri`
- **THEN** the system MUST return HTTP 422 with an error message indicating model_package_uri is required
- **THEN** no row MUST be inserted into `core.model_instance`

### Requirement: Model package validation

The CLI command `nhms-model validate-package <package_path>` MUST verify that a model package directory contains all required SHUD input files. Required files: at least one `.mesh` file, at least one `.para` file, and at least one `.calib` file. The validator MUST report missing files and return a non-zero exit code on failure.

#### Scenario: Valid model package passes validation

- **WHEN** a user runs `nhms-model validate-package ./my-model/` and the directory contains `basin.mesh`, `basin.para`, and `basin.calib`
- **THEN** the command MUST exit with code 0
- **THEN** stdout MUST include a message indicating all required files are present

#### Scenario: Model package missing required files

- **WHEN** a user runs `nhms-model validate-package ./my-model/` and the directory is missing the `.para` file
- **THEN** the command MUST exit with a non-zero exit code
- **THEN** stderr MUST list the missing file type(s) (e.g., `Missing required file: *.para`)

#### Scenario: API rejects model registration with invalid package

- **WHEN** a client sends `POST /api/v1/models` with a `package_s3_uri` pointing to a model package in object storage that is missing required files
- **THEN** the system MUST return HTTP 422 with a validation error listing the missing files
- **THEN** no `core.model_instance` row MUST be inserted

### Requirement: Model activation and deactivation

A model instance uses `active_flag` (BOOLEAN) to indicate whether it is eligible for forecast runs. Only model instances with `active_flag = true` SHALL be eligible for forecast runs. The `PUT /api/v1/models/{model_id}/active` endpoint MUST accept a JSON body `{"active": true}` or `{"active": false}` to set the `active_flag` accordingly.

#### Scenario: Activate a model instance

- **WHEN** a client sends `PUT /api/v1/models/{model_id}/active` with body `{"active": true}` for a model instance with `active_flag = false`
- **THEN** the `active_flag` column in `core.model_instance` MUST be updated to `true`
- **THEN** the response MUST return HTTP 200 with the updated model instance

#### Scenario: Deactivate an active model instance

- **WHEN** a client sends `PUT /api/v1/models/{model_id}/active` with body `{"active": false}` for a model instance with `active_flag = true`
- **THEN** the `active_flag` column MUST be updated to `false`
- **THEN** the response MUST return HTTP 200

#### Scenario: Reject activation of already active model

- **WHEN** a client sends `PUT /api/v1/models/{model_id}/active` with body `{"active": true}` for a model instance with `active_flag = true`
- **THEN** the system MUST return HTTP 409 Conflict with a message indicating the model is already active

#### Scenario: Reject forecast run with inactive model

- **WHEN** a forecast run is submitted referencing a `model_id` whose `active_flag = false`
- **THEN** the system MUST reject the run with HTTP 422 and a message indicating the model is not active

### Requirement: River segment crosswalk maintenance

The system SHALL maintain a `core.river_segment_crosswalk` table that maps internal `river_segment_id` values to external identifiers (e.g., national river codes). Each crosswalk entry MUST reference a valid `(river_segment_id, river_network_version_id)` composite key in `core.river_segment`. The crosswalk MUST support multiple external ID systems via a `source` column.

#### Scenario: Insert crosswalk entries for a river network version

- **WHEN** a river_network_version is registered with 10 river segments and each segment has a national river code
- **THEN** 10 rows MUST be inserted into `core.river_segment_crosswalk` with `source = 'national_river_code'`
- **THEN** each row MUST reference a valid `(river_segment_id, river_network_version_id)` in `core.river_segment`
- **THEN** a unique constraint on `(river_network_version_id, river_segment_id, source)` MUST prevent duplicate mappings

#### Scenario: Query external ID by internal segment ID

- **WHEN** a client queries the crosswalk for `river_segment_id = 'seg_001'` and `source = 'national_river_code'`
- **THEN** the system MUST return the corresponding `external_id` value
- **THEN** the lookup MUST be performant via an index on `(river_network_version_id, source, river_segment_id)`

### Requirement: Query active models

The `GET /api/v1/models` endpoint MUST return a paginated list of model instances. By default, only models with `active_flag = true` SHALL be returned. An optional `active` query parameter (boolean) MUST allow filtering by active or inactive models. An optional `active=all` value MUST return all models regardless of `active_flag`.

#### Scenario: List active models with default filter

- **WHEN** a client sends `GET /api/v1/models` without query parameters
- **THEN** the response MUST return HTTP 200 with a JSON array containing only model instances where `active_flag = true`
- **THEN** each item MUST include `model_id`, `basin_version_id`, `river_network_version_id`, `mesh_version_id`, `calibration_version_id`, `shud_code_version`, `model_package_uri`, `active_flag`, and `created_at`

#### Scenario: List inactive models with explicit filter

- **WHEN** a client sends `GET /api/v1/models?active=false`
- **THEN** the response MUST return only model instances with `active_flag = false`

#### Scenario: Paginated model listing

- **WHEN** there are 25 active models and a client sends `GET /api/v1/models?limit=10&offset=0`
- **THEN** the response MUST return exactly 10 model instances
- **THEN** the response MUST include a `total` field with value 25
- **THEN** a subsequent request with `offset=10` MUST return the next 10 models

### Requirement: Demo bootstrap for M1 milestone

For M1, the system SHALL provide a bootstrap script or API sequence that creates a complete demo dataset: one `core.basin`, one `core.basin_version` (with MultiPolygon 4490 geometry), one `core.river_network_version` with at least 3 `core.river_segment` rows (each with LineString 4490 geometry and segment_order), one mesh version, and one `core.model_instance` with `active_flag = true` and a valid `model_package_uri`.

#### Scenario: Bootstrap creates a fully linked demo model

- **WHEN** a developer runs the M1 bootstrap script
- **THEN** a `core.basin` row MUST be created with a demo basin_id and basin_name
- **THEN** a `core.basin_version` row MUST be created with a valid MultiPolygon 4490 geometry, `active_flag = true`, and `valid_from` set
- **THEN** a `core.river_network_version` row MUST be created referencing the basin_version, with `segment_count` matching the number of segments
- **THEN** at least 3 `core.river_segment` rows MUST be created with valid `segment_order`, `downstream_segment_id`, `length_m`, and LineString 4490 geometry
- **THEN** a mesh version row MUST be created referencing the basin_version
- **THEN** a `core.model_instance` row MUST be created referencing the basin_version, river_network_version, and mesh_version, with `active_flag = true` and a valid `model_package_uri`
- **THEN** the bootstrap MUST be idempotent (safe to run multiple times)

