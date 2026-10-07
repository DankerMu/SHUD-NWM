## MODIFIED Requirements

### Requirement: Met station query

The API SHALL provide `GET /api/v1/met/stations` to list meteorological proxy stations associated with a basin version or model instance. With `basin_version_id` alone, the list SHALL be the stations of that basin version that carry `met.interp_weight` rows for the model of its latest displayable forecast run (`run_type = 'forecast'`, status `succeeded`, `parsed` or `published`, non-null `cycle_time`; newest `cycle_time`, then highest `run_id`, any source), and SHALL NOT depend on `met.met_station.active_flag`. Stations of superseded model generations and legacy stations of models with no displayable run SHALL NOT be listed.

#### Scenario: Query stations by basin_version_id

- **WHEN** a client calls `GET /api/v1/met/stations?basin_version_id={id}`
- **THEN** the response MUST return HTTP 200 with the met stations of the model used by the basin version's latest displayable forecast run
- **THEN** each station MUST include `station_id`, `name`, `longitude`, `latitude`, and `elevation`
- **THEN** a station of that model MUST be listed whatever its `active_flag`, and an active station that the model has no `interp_weight` row for MUST NOT be listed

#### Scenario: Basin version with an older model generation

- **WHEN** the basin version has displayable forecast runs of two models and the newer run uses model B
- **THEN** only stations with `interp_weight` rows of model B are returned, and COUNT and page statements share the predicate

#### Scenario: Basin version without a displayable forecast run

- **WHEN** the basin version has no forecast run in `succeeded`, `parsed` or `published`
- **THEN** the response MUST return HTTP 200 with an empty list and `total_count` 0

#### Scenario: Query stations by model_id

- **WHEN** a client calls `GET /api/v1/met/stations?model_id={id}`
- **THEN** the response MUST return HTTP 200 with a list of met stations that have `interp_weight` for the specified model instance
- **THEN** the station set MUST match the stations used for forcing interpolation in that model

#### Scenario: Query with no filter returns 422

- **WHEN** a client calls `GET /api/v1/met/stations` without any filter parameter
- **THEN** the response MUST return HTTP 422 with error code `MISSING_REQUIRED_FILTER`
- **THEN** the error message MUST indicate that at least one of `basin_version_id` or `model_id` is required
