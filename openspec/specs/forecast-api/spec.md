# forecast-api Specification

## Purpose
TBD - created by archiving change m1-gfs-forecast-loop. Update Purpose after archive.
## Requirements
### Requirement: Forecast series query

The API SHALL provide `GET /api/v1/basin-versions/{basin_version_id}/river-segments/{segment_id}/forecast-series` to return forecast flow time series for a given river segment within a basin version. The response MUST include the segment identifier, issue time, unit, and one or more scenario series with timestamped data points and frequency thresholds.

#### Scenario: Query latest forecast for a segment

- **WHEN** a client calls `GET /api/v1/basin-versions/{basin_version_id}/river-segments/{segment_id}/forecast-series?issue_time=latest&variables=q_down,stage&scenarios=GFS`
- **THEN** the response MUST return HTTP 200 with a JSON body containing:
  - `segment_id`: the requested river segment identifier (e.g., `yangtze_v12_riv_000123`)
  - `issue_time`: the ISO 8601 UTC timestamp of the most recent forecast cycle
  - `unit`: `"m3/s"`
  - `series`: an array with at least one entry containing `scenario_id`, `segment_role`, and `points`
  - `frequency_thresholds`: an object with frequency threshold data
- **THEN** the `scenario_id` MUST be `"forecast_gfs_deterministic"` for M1
- **THEN** the `segment_role` MUST be `"future_7_days"`
- **THEN** `points` MUST be an array of `[timestamp, value]` tuples (two-element arrays), where `timestamp` is ISO 8601 UTC and `value` is a float in m3/s

#### Scenario: Query forecast for a specific issue_time

- **WHEN** a client calls `GET /api/v1/basin-versions/{basin_version_id}/river-segments/{segment_id}/forecast-series?issue_time=2026-05-07T00:00:00Z&variables=q_down`
- **THEN** the response MUST return the forecast series corresponding to the specified cycle time
- **THEN** if no forecast exists for the specified issue_time, the response MUST return HTTP 404 with error code `RUN_NOT_PUBLISHED`

#### Scenario: Query for non-existent segment returns 404

- **WHEN** a client calls `GET /api/v1/basin-versions/{basin_version_id}/river-segments/{segment_id}/forecast-series` with an invalid segment_id
- **THEN** the response MUST return HTTP 404 with error code `SEGMENT_NOT_FOUND`

#### Scenario: Query for non-existent source returns 404

- **WHEN** a client calls the forecast-series endpoint with an invalid basin_version_id
- **THEN** the response MUST return HTTP 404 with error code `SOURCE_NOT_FOUND`

#### Scenario: Points are ordered by timestamp ascending

- **WHEN** the forecast series response contains points
- **THEN** the `points` array MUST be sorted by timestamp (first element of each tuple) in ascending order
- **THEN** the time span MUST cover approximately 7 days from the issue_time

---

### Requirement: Run query

The API SHALL provide endpoints to query hydro runs by ID or by filter criteria. Run metadata includes basin, data source, cycle time, status, and timing information.

#### Scenario: Get a specific run by ID

- **WHEN** a client calls `GET /api/v1/runs/{run_id}`
- **THEN** the response MUST return HTTP 200 with a JSON body containing:
  - `run_id`, `basin_id`, `source`, `cycle_time` (ISO 8601), `status`, `created_at`, `updated_at`
- **THEN** if the run_id does not exist, the response MUST return HTTP 404 with error code `RUN_NOT_FOUND`

#### Scenario: List runs with filter criteria

- **WHEN** a client calls `GET /api/v1/runs?basin_id=changjiang_demo&source=gfs&cycle_time=2026-05-07T00:00:00Z&status=succeeded`
- **THEN** the response MUST return HTTP 200 with a paginated list of runs matching all provided filters
- **THEN** each run in the list MUST include `run_id`, `basin_id`, `source`, `cycle_time`, `status`, `created_at`
- **THEN** all filter parameters are optional; omitting a filter MUST return runs without that constraint

#### Scenario: List runs with no matches returns empty list

- **WHEN** a client calls `GET /api/v1/runs` with filter criteria that match no runs
- **THEN** the response MUST return HTTP 200 with an empty `items` array and `total_count: 0`

---

### Requirement: Data source and cycle query

The API SHALL provide endpoints to list registered data sources and query forecast cycles for a specific source. This enables the frontend and operators to discover available data and monitor cycle ingestion status.

#### Scenario: List all data sources

- **WHEN** a client calls `GET /api/v1/data-sources`
- **THEN** the response MUST return HTTP 200 with a list of registered data sources
- **THEN** each entry MUST include `source_id`, `provider`, `source`, `format`, and `description`
- **THEN** for M1, at least one entry with `source="gfs"` and `provider="NOAA/NCEP"` MUST be present

#### Scenario: Query cycles for a data source with time range

- **WHEN** a client calls `GET /api/v1/data-sources/{source_id}/cycles?from=2026-05-01T00:00:00Z&to=2026-05-07T23:59:59Z&status=canonical_ready`
- **THEN** the response MUST return HTTP 200 with a paginated list of forecast cycles
- **THEN** each cycle MUST include `cycle_time` (ISO 8601), `status`, `file_count`, and `quality_flag`
- **THEN** results MUST be filtered to the specified time range and status

#### Scenario: Query cycles for non-existent source returns 404

- **WHEN** a client calls `GET /api/v1/data-sources/{source_id}/cycles` with an invalid source_id
- **THEN** the response MUST return HTTP 404 with error code `SOURCE_NOT_FOUND`

---

### Requirement: Met station query

The API SHALL provide `GET /api/v1/met/stations` to list meteorological proxy stations associated with a basin version or model instance. This supports debugging of forcing production by showing which stations are used for interpolation.

#### Scenario: Query stations by basin_version_id

- **WHEN** a client calls `GET /api/v1/met/stations?basin_version_id={id}`
- **THEN** the response MUST return HTTP 200 with a list of met stations
- **THEN** each station MUST include `station_id`, `name`, `longitude`, `latitude`, and `elevation`

#### Scenario: Query stations by model_id

- **WHEN** a client calls `GET /api/v1/met/stations?model_id={id}`
- **THEN** the response MUST return HTTP 200 with a list of met stations that have `interp_weight` for the specified model instance
- **THEN** the station set MUST match the stations used for forcing interpolation in that model

#### Scenario: Query with no filter returns 422

- **WHEN** a client calls `GET /api/v1/met/stations` without any filter parameter
- **THEN** the response MUST return HTTP 422 with error code `MISSING_REQUIRED_FILTER`
- **THEN** the error message MUST indicate that at least one of `basin_version_id` or `model_id` is required

---

### Requirement: Error response format

All API endpoints SHALL use a unified error response format with a unique request_id for traceability. Error responses MUST include a structured error code and human-readable message.

#### Scenario: Error response contains required fields

- **WHEN** any API endpoint returns an error (4xx or 5xx)
- **THEN** the response body MUST contain:
  - `request_id`: a unique UUID for the request
  - `error`: an object with `code` (string, UPPER_SNAKE_CASE), `message` (human-readable string), and optional `details` (array of field-level errors)
- **THEN** the `Content-Type` MUST be `application/json`

#### Scenario: Request ID is present on success responses

- **WHEN** any API endpoint returns a successful response (2xx)
- **THEN** the response headers MUST include `X-Request-ID` with a unique UUID
- **THEN** the same request_id MUST be used in server-side logs for correlation

#### Scenario: Validation error returns 422 with field details

- **WHEN** a client sends a request with invalid query parameters (e.g., non-ISO-8601 date string)
- **THEN** the response MUST return HTTP 422
- **THEN** the `error.code` MUST be `VALIDATION_ERROR`
- **THEN** the `error.details` MUST list each invalid field with its name, the rejected value, and a reason

#### Scenario: Internal server error returns 500 with request_id

- **WHEN** an unhandled exception occurs during request processing
- **THEN** the response MUST return HTTP 500
- **THEN** the `error.code` MUST be `INTERNAL_ERROR`
- **THEN** the `error.message` MUST NOT expose stack traces or internal implementation details
- **THEN** the `request_id` MUST still be present for diagnostic correlation

---

### Requirement: Response pagination

All list endpoints SHALL support cursor-based or offset-based pagination to handle large result sets. Pagination parameters and metadata MUST be consistent across all list endpoints.

#### Scenario: Default pagination is applied

- **WHEN** a client calls a list endpoint without pagination parameters
- **THEN** the response MUST return at most 50 items (default page size)
- **THEN** the response MUST include `total_count`, `limit`, and `offset` metadata fields

#### Scenario: Custom pagination parameters are respected

- **WHEN** a client calls a list endpoint with `limit=10&offset=20`
- **THEN** the response MUST return at most 10 items starting from offset 20
- **THEN** `total_count` MUST reflect the total matching records (not just the returned page)

#### Scenario: Pagination limit is bounded

- **WHEN** a client calls a list endpoint with `limit=1000`
- **THEN** the response MUST cap the limit at the maximum allowed value (default 200)
- **THEN** the returned `limit` field MUST reflect the capped value, not the requested value

#### Scenario: Empty page beyond total count returns empty list

- **WHEN** a client requests an offset beyond the total number of results
- **THEN** the response MUST return HTTP 200 with an empty `items` array
- **THEN** `total_count` MUST still reflect the total matching records

