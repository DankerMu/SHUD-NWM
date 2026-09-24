## ADDED Requirements

### Requirement: Run responses expose only the public HydroRun projection

`GET /api/v1/runs/{run_id}` and `GET /api/v1/runs` SHALL return run objects
built from one explicit public field allowlist, the same for detail and list.
The store SHALL select the allowlisted `hydro.hydro_run` columns by name, never
`h.*`, and both routes SHALL declare the shared `HydroRun` response model. That
model drops unknown keys at runtime, and its OpenAPI schema declares no
additional properties. Existing public fields, including `run_key`, `parsed_at`,
`basin_id`, `river_network_version_id` and `source`, SHALL keep their names and
JSON value shapes.

#### Scenario: a hydro_run row carries an internal column

- **WHEN** the store row for a run carries `timeseries_store` or another column
  outside the allowlist
- **THEN** neither the detail `data` nor any list `items[]` object contains that
  key, and every allowlisted field is still present with its previous value
  shape.
