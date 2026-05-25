## ADDED Requirements

### Requirement: Station forcing series route

The system SHALL expose a bounded station forcing time-series route backed by `met.forcing_station_timeseries`.

#### Scenario: Explicit forcing version query
- **WHEN** a client requests `GET /api/v1/met/stations/{station_id}/series` with a valid `forcing_version_id`
- **THEN** the response includes the requested `station_id`, `forcing_version_id`, `source_id`, `cycle_time` when available, and grouped series for the requested variables
- **AND** each variable series includes `variable`, `unit`, `points`, and `truncated`
- **AND** each point includes `valid_time`, `value`, and `quality_flag`.

#### Scenario: Model source cycle resolution
- **WHEN** a client requests station series with `model_id`, `source_id`, and `cycle_time` but without `forcing_version_id`
- **THEN** the API resolves the matching forcing version for that model/source/cycle
- **AND** the response uses the resolved `forcing_version_id` or returns a typed unavailable/not-found error when no matching forcing version exists.

#### Scenario: Required MVP variables
- **WHEN** station series is requested without an explicit variable filter
- **THEN** the API returns available series for `PRCP`, `TEMP`, `RH`, `wind`, `Rn`, and `Press`
- **AND** it does not invent variables or samples that are absent from the database.

#### Scenario: Variable and time filtering
- **WHEN** a client supplies `variables`, `from`, `to`, or `limit`
- **THEN** the API applies those filters before returning chart points
- **AND** invalid variables, invalid time ranges, or invalid limits produce validation errors rather than broad unbounded queries.

#### Scenario: Truncation metadata
- **WHEN** more points exist than the requested or default limit allows
- **THEN** the response marks the affected variable series as `truncated=true`
- **AND** exposes `limit`, `returned_points`, `requested_from`, `requested_to`, `returned_from`, and `returned_to` metadata for the UI to show that only a bounded sample was returned.

#### Scenario: Missing station or forcing version
- **WHEN** `station_id` or `forcing_version_id` does not exist
- **THEN** the API returns a stable not-found or unavailable response
- **AND** it does not return an empty success response that could be confused with a station that has a valid but empty time range.

### Requirement: Station series contract stays aligned across API, store, and OpenAPI

The station series contract SHALL be implemented consistently in backend query helpers, FastAPI routes, OpenAPI, frontend generated types, and tests.

#### Scenario: OpenAPI drift prevention
- **WHEN** the station series route is implemented
- **THEN** `openapi/nhms.v1.yaml` contains the route, query parameters, response schemas, and error schemas
- **AND** the route is removed from any deferred OpenAPI drift allowlist.

#### Scenario: Store query preserves provenance
- **WHEN** the backend query helper reads station series
- **THEN** it preserves `unit`, `native_resolution` when available, `quality_flag`, `source_id`, `valid_time`, and forcing version provenance from the database.

#### Scenario: QHH station completeness verification
- **WHEN** QHH forcing versions are validated for MVP readiness
- **THEN** each selected QHH MVP forcing version records expected and actual station counts, with actual count near the 386 seeded forcing stations unless an explicit scoped reason is recorded
- **AND** at least one station and the aggregate coverage check confirm `PRCP`, `TEMP`, `RH`, `wind`, `Rn`, and `Press` have units and quality flags
- **AND** a readiness check records missing station, missing variable, missing unit, missing quality flag, and query-index findings without treating already implemented forcing writes as a new producer requirement.
