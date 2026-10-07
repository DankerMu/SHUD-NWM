## ADDED Requirements

### Requirement: A model whose artifacts have left the store is a 404, never mapped to another model

The series reader SHALL use the `model_id` it is given as the path segment and MUST NOT map it to another
model. A `model_id` with no artifact directory in the requested cycle SHALL answer HTTP 404
`STATION_FORCING_FILE_NOT_FOUND`, including a legacy baseline model id (`basins_<slug>_shud`) of a basin
whose retained cycles hold only Direct Grid (`dg_*`) artifacts. Callers obtain the model id of the product
they display and pass it.

#### Scenario: Legacy model id on a Direct Grid store

- **WHEN** the API receives a station of a basin, a retained `cycle_time`, and the basin's legacy model id,
  and the cycle directory of that basin holds only a `dg_*` model directory
- **THEN** the API SHALL return HTTP 404 with code `STATION_FORCING_FILE_NOT_FOUND`

#### Scenario: Direct Grid model id and one of its stations

- **WHEN** the API receives a `dg_*` model id present in a retained cycle, a station that
  `met.interp_weight` holds for that model, and the matching `source_id` and `cycle_time`
- **THEN** the API SHALL return HTTP 200 with a non-empty series read from
  `forcing/{source}/{cycle}/{basin_version_id}/{model_id}/shud/{forcing_filename}`
