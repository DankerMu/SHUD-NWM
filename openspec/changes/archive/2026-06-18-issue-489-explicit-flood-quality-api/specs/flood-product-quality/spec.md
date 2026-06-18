# flood-product-quality Spec Delta

## ADDED Requirements

### Requirement: API reads explicit flood run quality

Flood API and forecast-store readiness SHALL prefer explicit
`flood.run_product_quality` fields when the table and fields are available.

#### Scenario: all-no-curve explicit unavailable quality

- **GIVEN** a frequency-ready run has q_down rows
- **AND** `flood.run_product_quality` marks flood return-period quality
  unavailable with no-curve blockers
- **WHEN** API layer catalog or forecast latest-product quality is requested
- **THEN** q_down/discharge remains available
- **AND** flood return-period quality is reported unavailable with explicit
  `unavailable_products` and `residual_blockers`
- **AND** explicit counters such as expected rows, meaningful rows, and
  no-curve rows are preserved in the response where product quality details are
  returned.

#### Scenario: partial-curve explicit degraded quality

- **GIVEN** a run has some meaningful return-period rows
- **AND** explicit quality says expected coverage exceeds meaningful coverage
- **WHEN** product quality is read
- **THEN** flood quality is not reported as ready
- **AND** the response preserves expected/meaningful/no-curve evidence such as
  `expected_result_rows`, `meaningful_result_rows`, and
  `no_frequency_curve_rows`.

### Requirement: Flood route gates use run quality before source-row identity

Flood return-period and warning-level routes SHALL return product-unavailable
errors for explicit unavailable runs before returning source-row identity
errors caused by absent result rows.

#### Scenario: unavailable run with zero result rows

- **GIVEN** explicit run quality is unavailable
- **AND** `flood.return_period_result` has no rows for the requested run
- **WHEN** a flood tile or GeoJSON route is requested
- **THEN** the response is 409 `FLOOD_PRODUCT_UNAVAILABLE`
- **AND** error details include explicit quality evidence
- **AND** the response is not a misleading source identity 404.

### Requirement: Flood ready-run discovery uses explicit ready state

Latest flood-ready run discovery SHALL select only runs whose explicit flood
quality is ready when explicit quality is available.

#### Scenario: latest frequency-ready run has unavailable flood quality

- **GIVEN** the newest frequency-ready run has explicit flood quality
  unavailable
- **AND** an older run has explicit flood quality ready
- **WHEN** latest flood-ready run discovery runs
- **THEN** it selects the older ready flood run
- **AND** layer catalog discovery can still use the newest frequency-ready run
  for q_down/discharge.

### Requirement: Explicit quality compatibility is deterministic

Readers SHALL use explicit quality when the table and fields are available and
SHALL keep legacy lightweight fallback only for missing-table or pre-explicit
schema compatibility. q_down SHALL NOT be marked unavailable solely because
flood quality storage is missing.

#### Scenario: explicit table exists but run row is missing

- **GIVEN** `flood.run_product_quality` exists with explicit fields
- **AND** no quality row exists for the run
- **WHEN** flood quality is read
- **THEN** flood return-period quality is unavailable or missing quality
- **AND** q_down/discharge readiness is not marked unavailable solely because of
  that missing flood quality row.

#### Scenario: read replica lacks run_product_quality

- **GIVEN** a read replica cannot join `flood.run_product_quality`
- **WHEN** forecast-store flood quality is selected
- **THEN** the legacy row-existence fallback remains deterministic
- **AND** q_down product readiness is not marked unavailable solely by that
  absence.

#### Scenario: read replica has pre-explicit quality schema

- **GIVEN** `flood.run_product_quality` exists but lacks explicit quality fields
- **WHEN** flood quality is selected
- **THEN** the legacy count fallback remains deterministic
- **AND** q_down product readiness is not marked unavailable solely by that
  schema gap.
