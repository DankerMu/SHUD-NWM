# OpenAPI Contract

Capability: `openapi-contract`
Status: draft
Parent: m0-engineering-init

## ADDED Requirements

### Requirement: OpenAPI specification file exists and is valid

The file `openapi/nhms.v1.yaml` MUST be a valid OpenAPI 3.0.x document that serves as the single source of truth for all API contracts until FastAPI auto-generation replaces it in M1.

#### Scenario: Specification file passes validation

WHEN a developer runs an OpenAPI validator (e.g., `openapi-spec-validator openapi/nhms.v1.yaml`)
THEN the validator MUST report zero errors
AND the document MUST declare `openapi: "3.0.3"` (or compatible 3.0.x version)
AND the `info.title` MUST be "National Hydrological Simulation System API" or its Chinese equivalent
AND `info.version` MUST be "1.0.0"

#### Scenario: Specification uses the correct API version prefix

WHEN a developer inspects the `servers` section
THEN at least one server entry MUST include the base path `/api/v1`
AND all `paths` keys MUST be prefixed with `/api/v1/`

### Requirement: All core component schemas are defined

The `components.schemas` section MUST define data models for all core entities referenced by the API endpoints. Each schema MUST include `type`, `properties`, and `required` fields.

#### Scenario: Basin schema is defined

WHEN a developer inspects `components.schemas.Basin`
THEN it MUST include properties: `basin_id` (string), `basin_name` (string), `basin_group` (string, nullable), `description` (string, nullable), `created_at` (string, format: date-time)
AND `required` MUST include at minimum: `basin_id`, `basin_name`, `created_at`

#### Scenario: BasinVersion schema is defined with geometry

WHEN a developer inspects `components.schemas.BasinVersion`
THEN it MUST include properties: `basin_version_id` (string), `basin_id` (string), `version_label` (string), `geom` (object, GeoJSON MultiPolygon), `active_flag` (boolean), `valid_from` (string, format: date-time, nullable), `valid_to` (string, format: date-time, nullable), `source_uri` (string, nullable), `checksum` (string, nullable), `created_at` (string, format: date-time)
AND `required` MUST include: `basin_version_id`, `basin_id`, `version_label`, `active_flag`

#### Scenario: ModelInstance schema is defined

WHEN a developer inspects `components.schemas.ModelInstance`
THEN it MUST include properties: `model_id` (string), `basin_version_id` (string), `river_network_version_id` (string), `mesh_version_id` (string), `calibration_version_id` (string), `shud_code_version` (string), `rshud_code_version` (string, nullable), `autoshud_code_version` (string, nullable), `active_flag` (boolean), `container_image` (string, nullable), `model_package_uri` (string), `resource_profile` (object), `created_at` (string, format: date-time)
AND `required` MUST include: `model_id`, `basin_version_id`, `shud_code_version`, `active_flag`

#### Scenario: MetStation schema is defined with geometry

WHEN a developer inspects `components.schemas.MetStation`
THEN it MUST include properties: `station_id` (string), `basin_version_id` (string), `station_name` (string, nullable), `geom` (object, GeoJSON Point), `elevation_m` (number, nullable), `station_role` (string), `active_flag` (boolean), `properties_json` (object, nullable), `created_at` (string, format: date-time)
AND `required` MUST include: `station_id`, `basin_version_id`, `active_flag`

#### Scenario: RiverSegment schema is defined

WHEN a developer inspects `components.schemas.RiverSegment`
THEN it MUST include properties: `river_segment_id` (string), `river_network_version_id` (string), `segment_order` (integer, nullable), `downstream_segment_id` (string, nullable), `length_m` (number, nullable), `geom` (object, GeoJSON LineString), `properties_json` (object), `created_at` (string, format: date-time)
AND `required` MUST include: `river_segment_id`, `river_network_version_id`

#### Scenario: HydroRun schema is defined with ENUM references

WHEN a developer inspects `components.schemas.HydroRun`
THEN it MUST include properties: `run_id` (string), `run_type` (string, enum: [analysis, forecast, hindcast]), `scenario_id` (string), `model_id` (string), `basin_version_id` (string), `forcing_version_id` (string, nullable), `init_state_id` (string, nullable), `source_id` (string, nullable), `cycle_time` (string, format: date-time, nullable), `status` (string, enum: [created, staged, submitted, running, succeeded, parsed, frequency_done, published, failed, cancelled, superseded]), `slurm_job_id` (string, nullable), `start_time` (string, format: date-time), `end_time` (string, format: date-time), `run_manifest_uri` (string, nullable), `output_uri` (string, nullable), `log_uri` (string, nullable), `error_code` (string, nullable), `error_message` (string, nullable), `created_at` (string, format: date-time), `updated_at` (string, format: date-time)
AND the `run_type` enum values MUST exactly match `hydro.run_type`
AND the `status` enum values MUST exactly match `hydro.run_status`

#### Scenario: ForcingVersion schema is defined

WHEN a developer inspects `components.schemas.ForcingVersion`
THEN it MUST include properties: `forcing_version_id` (string), `model_id` (string), `source_id` (string), `cycle_time` (string, format: date-time, nullable), `start_time` (string, format: date-time), `end_time` (string, format: date-time), `station_count` (integer), `forcing_package_uri` (string), `checksum` (string, nullable), `lineage_json` (object, nullable), `created_at` (string, format: date-time)
AND `required` MUST include: `forcing_version_id`, `model_id`, `source_id`, `start_time`, `end_time`

#### Scenario: RiverSeriesResponse schema matches the design document

WHEN a developer inspects `components.schemas.RiverSeriesResponse`
THEN it MUST include properties: `segment_id` (string), `issue_time` (string, format: date-time), `unit` (string), `series` (array of SeriesSegment), `frequency_thresholds` (object with Q2/Q5/Q10/Q20/Q50/Q100)
AND the `SeriesSegment` schema MUST include: `scenario_id` (string), `segment_role` (string), `points` (array of [time, value] tuples)

#### Scenario: FloodAlertSummary schema is defined

WHEN a developer inspects `components.schemas.FloodAlertSummary`
THEN it MUST include properties: `run_id` (string), `threshold` (string), `total_segments` (integer), `alert_counts` (object with keys: normal, elevated, watch, warning, high_risk, severe, extreme), `updated_at` (string, format: date-time)

#### Scenario: PipelineStage schema is defined

WHEN a developer inspects `components.schemas.PipelineStage`
THEN it MUST include properties: `stage` (string), `status` (string, enum: [pending, running, succeeded, partially_failed, failed, skipped]), `started_at` (string, format: date-time, nullable), `finished_at` (string, format: date-time, nullable), `duration_seconds` (integer, nullable), `basin_progress` (string)

#### Scenario: PipelineJob schema is defined

WHEN a developer inspects `components.schemas.PipelineJob`
THEN it MUST include properties: `job_id` (string), `job_type` (string), `source` (string), `cycle_time` (string, format: date-time), `model_id` (string, nullable), `status` (string), `slurm_job_id` (string, nullable), `submitted_at` (string, format: date-time), `finished_at` (string, format: date-time, nullable)

#### Scenario: QcResult schema is defined

WHEN a developer inspects `components.schemas.QcResult`
THEN it MUST include properties: `qc_id` (integer), `qc_checkpoint` (string), `target_type` (string), `target_id` (string), `run_id` (string, nullable), `passed` (boolean), `severity` (string), `checks_json` (object), `message` (string, nullable), `created_at` (string, format: date-time)

#### Scenario: ErrorResponse schema is defined

WHEN a developer inspects `components.schemas.ErrorResponse`
THEN it MUST include properties: `request_id` (string), `status` (string, const: "error"), `error` (object)
AND the `error` object MUST include: `code` (string), `message` (string), `details` (object, nullable)

### Requirement: Standard response envelope is used consistently

All successful responses MUST use the standard envelope defined in `docs/spec/04_api_design.md` section 2.

#### Scenario: Successful responses use the standard envelope

WHEN a developer inspects any `200` or `201` response schema in the specification
THEN it MUST include properties: `request_id` (string), `status` (string, example: "ok"), `data` (object or array)
AND the `data` field MUST contain or reference the appropriate domain schema

#### Scenario: Error responses use the ErrorResponse schema

WHEN a developer inspects any `4xx` or `5xx` response definition
THEN it MUST reference `components.schemas.ErrorResponse`
AND the response MUST include `application/json` as the content type

### Requirement: Security scheme is defined

The specification MUST include a security scheme placeholder for Bearer token authentication.

#### Scenario: Bearer token security scheme exists

WHEN a developer inspects `components.securitySchemes`
THEN a scheme named `BearerAuth` (or equivalent) MUST exist
AND it MUST be of type `http` with scheme `bearer` and bearerFormat `JWT`
AND the top-level `security` section MUST reference this scheme as the default

#### Scenario: Security can be overridden per endpoint

WHEN a developer inspects individual path operations
THEN each operation MAY override the global security to `[]` (no auth) for public endpoints
AND the specification MUST include a comment or description indicating that auth implementation is deferred to M1+

### Requirement: Core resource endpoints from sections 3-9 are defined

The specification MUST include path definitions for all core endpoints listed in `docs/spec/04_api_design.md` sections 3 through 9.

#### Scenario: Basin and version endpoints are defined (section 3)

WHEN a developer inspects the `paths` section
THEN the following paths MUST exist:
  - `GET /api/v1/basins` -- returns list of basins
  - `GET /api/v1/basins/{basin_id}/versions` -- returns versions of a basin
AND each operation MUST have `operationId`, `summary`, `tags`, and response schemas

#### Scenario: Model and asset endpoints are defined (section 6)

WHEN a developer inspects the `paths` section
THEN the following paths MUST exist:
  - `GET /api/v1/models` -- with query parameter `basin_version_id` and `active`
  - `GET /api/v1/models/{model_id}` -- returns model instance detail
  - `GET /api/v1/models/{model_id}/versions` -- returns version history for a model
  - `GET /api/v1/models/{model_id}/states` -- returns available model state snapshots
  - `GET /api/v1/models/{model_id}/flood-frequency-curves` -- returns frequency curves
  - `GET /api/v1/basin-versions/{basin_version_id}/river-network-versions` -- returns river network versions
  - `PUT /api/v1/models/{model_id}/active` -- toggles active flag
AND each endpoint MUST define appropriate request parameters and response schemas

#### Scenario: Data source and cycle endpoints are defined (section 3)

WHEN a developer inspects the `paths` section
THEN the following paths MUST exist:
  - `GET /api/v1/data-sources` -- returns list of data sources
  - `GET /api/v1/data-sources/{source_id}/cycles` -- with query parameters `from`, `to`, `status`

#### Scenario: River segment and forecast series endpoints are defined (sections 3-4)

WHEN a developer inspects the `paths` section
THEN the following paths MUST exist:
  - `GET /api/v1/basin-versions/{basin_version_id}/river-segments/{segment_id}` -- returns segment detail
  - `GET /api/v1/basin-versions/{basin_version_id}/river-segments/{segment_id}/forecast-series` -- with query parameters `issue_time`, `variables`, `scenarios`
AND the forecast-series response MUST reference `RiverSeriesResponse`

#### Scenario: Met station endpoints are defined (section 3)

WHEN a developer inspects the `paths` section
THEN the following paths MUST exist:
  - `GET /api/v1/met/stations` -- with query parameters `basin_version_id`, `model_id`
  - `GET /api/v1/met/stations/{station_id}/series` -- with query parameters `forcing_version_id`, `variables`

#### Scenario: Run management endpoints are defined (section 3)

WHEN a developer inspects the `paths` section
THEN the following paths MUST exist:
  - `GET /api/v1/runs/{run_id}` -- returns run detail referencing HydroRun schema
  - `GET /api/v1/runs` -- with query parameters `basin_id`, `source`, `cycle_time`, `status`

#### Scenario: Tile endpoints are defined (section 5)

WHEN a developer inspects the `paths` section
THEN the following paths MUST exist:
  - `GET /api/v1/tiles/river-network/{basin_version_id}/{z}/{x}/{y}.pbf`
  - `GET /api/v1/tiles/hydro/{run_id}/{variable}/{valid_time}/{z}/{x}/{y}.pbf`
  - `GET /api/v1/tiles/flood-return-period/{run_id}/{duration}/{valid_time}/{z}/{x}/{y}.pbf`
  - `GET /api/v1/tiles/met/{product_id}/{variable}/{valid_time}/{z}/{x}/{y}.png`
AND tile endpoints MUST define `z`, `x`, `y` as integer path parameters
AND the response content type MUST be `application/x-protobuf` for `.pbf` and `image/png` for `.png`
AND tile endpoint responses are EXEMPT from the standard JSON response envelope; they return raw binary content directly

#### Scenario: Metrics and queue endpoints are defined (section 7)

WHEN a developer inspects the `paths` section
THEN the following paths MUST exist:
  - `GET /api/v1/metrics/stage-duration` -- returns P50/P95 duration per pipeline stage, with query parameters `source`, `from`, `to`
  - `GET /api/v1/metrics/success-rate` -- returns success/failure ratio over time, with query parameters `source`, `from`, `to`, `granularity`
  - `GET /api/v1/queue/depth` -- returns current Slurm queue depth by priority
AND each endpoint MUST define appropriate response schemas

#### Scenario: Pipeline and monitoring endpoints are defined (section 7)

WHEN a developer inspects the `paths` section
THEN the following paths MUST exist:
  - `GET /api/v1/pipeline/status` -- with query parameters `source`, `cycle_time`
  - `GET /api/v1/pipeline/stages` -- with query parameters `source`, `cycle_time`
  - `GET /api/v1/jobs` -- with query parameters `source`, `cycle_time`, `status`, `model_id`, `limit`, `offset`
  - `GET /api/v1/jobs/{job_id}/logs`
  - `POST /api/v1/runs/{run_id}/retry`
  - `POST /api/v1/runs/{run_id}/cancel`
  - `GET /api/v1/queue/depth`
AND the stages response MUST reference `PipelineStage` schema
AND the jobs response MUST reference `PipelineJob` schema

#### Scenario: Flood alert endpoints are defined (section 8)

WHEN a developer inspects the `paths` section
THEN the following paths MUST exist:
  - `GET /api/v1/flood-alerts/summary` -- with query parameters `run_id`, `threshold`
  - `GET /api/v1/flood-alerts/ranking` -- with query parameters `run_id`, `limit`
  - `GET /api/v1/flood-alerts/segments` -- with query parameters `run_id`, `min_return_period`, `valid_time`
  - `GET /api/v1/flood-alerts/timeline` -- with query parameters `run_id`, `segment_id`
AND the summary response MUST reference `FloodAlertSummary` schema

#### Scenario: Lineage endpoints are defined (section 9)

WHEN a developer inspects the `paths` section
THEN the following paths MUST exist:
  - `GET /api/v1/lineage/river-point` -- with query parameters `run_id`, `segment_id`, `valid_time`, `variable`
  - `GET /api/v1/lineage/forcing-point` -- with query parameters `forcing_version_id`, `station_id`, `valid_time`, `variable`
  - `GET /api/v1/lineage/product/{product_id}`

### Requirement: Layer and map metadata endpoints are defined

The specification MUST include endpoints for map layer discovery.

#### Scenario: Layer listing and time navigation endpoints exist

WHEN a developer inspects the `paths` section
THEN the following paths MUST exist:
  - `GET /api/v1/layers` -- returns available map layers
  - `GET /api/v1/layers/{layer_id}/valid-times` -- returns available time steps for a layer
AND each endpoint MUST have defined response schemas

### Requirement: All time fields use ISO 8601 UTC format

Every date-time field across all schemas MUST use the `format: date-time` specifier, ensuring ISO 8601 UTC representation.

#### Scenario: Date-time fields are consistently formatted

WHEN a developer searches for all properties with date/time semantics across all schemas
THEN every such property MUST have `type: string` and `format: date-time`
AND the description MUST state or imply UTC timezone
AND no property MUST use Unix timestamps or non-ISO date formats

### Requirement: Role-based access control roles are documented

The specification MUST define the set of authorization roles used by the system so that endpoint-level access control can reference them.

#### Scenario: Six standard roles are defined in security documentation

WHEN a developer inspects the security section or `components.securitySchemes` descriptions
THEN the specification MUST document the following 6 roles:
  - `viewer` -- read-only access to public dashboards and published results
  - `analyst` -- read access to detailed data, timeseries, and lineage
  - `operator` -- can trigger retries, cancel runs, and manage pipeline operations
  - `model_admin` -- can create/update models, basin versions, and calibration data
  - `sys_admin` -- full system administration including user and role management
  - `developer` -- access to development and debugging endpoints (mock reset, logs)
AND each endpoint SHOULD document which roles are permitted (enforcement deferred to M1+)
AND the role definitions MUST be included in the `info.description` or a dedicated `x-roles` extension

### Requirement: API non-functional requirements are specified

The specification MUST include non-functional performance targets from section 12 of the design document.

#### Scenario: P95 latency targets are documented per endpoint category

WHEN a developer inspects the specification metadata (via `info.description` or `x-nfr` extension)
THEN the following P95 latency targets MUST be stated:
  - List/query endpoints (e.g., `GET /basins`, `GET /runs`): P95 <= 200ms
  - Detail endpoints (e.g., `GET /models/{model_id}`): P95 <= 100ms
  - Timeseries endpoints (e.g., forecast-series, met station series): P95 <= 500ms
  - Tile endpoints (vector/raster): P95 <= 300ms
  - Pipeline status/stages: P95 <= 200ms
  - Flood alert endpoints: P95 <= 300ms
AND these targets apply under normal load conditions as defined in the design document
