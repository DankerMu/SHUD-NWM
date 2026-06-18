# flood-product-quality Specification

## Purpose
TBD - created by archiving change issue-487-run-product-quality-source. Update Purpose after archive.
## Requirements
### Requirement: Run-level flood quality source of truth

`flood.run_product_quality` SHALL be able to represent flood product quality for
a hydro run without requiring any `flood.return_period_result` rows.

#### Scenario: all segments lack frequency curves

- **GIVEN** a hydro run has expected peak/timestep flood product coverage
- **AND** no segment has a usable frequency curve
- **WHEN** explicit run product quality is written
- **THEN** `flood.run_product_quality` contains one row for the run
- **AND** `quality_state` is `unavailable`
- **AND** no-curve counters and residual blockers identify the missing curve
  reason
- **AND** each residual blocker preserves `code`, `state`, `quality_flag`,
  `residual_risk`, and `run_id`
- **AND** meaningful return-period/warning counters are zero.

#### Scenario: partial frequency curve coverage

- **GIVEN** a hydro run has expected flood product coverage
- **AND** only some segments have usable frequency curves
- **WHEN** explicit run product quality is written
- **THEN** expected counters are greater than meaningful counters
- **AND** `quality_state` is not `ready`
- **AND** unavailable products or residual blockers identify the missing
  coverage.

### Requirement: Historical backfill compatibility

The quality helper SHALL continue to derive compatible count fields from
existing `flood.return_period_result` rows.

#### Scenario: legacy source rows exist

- **GIVEN** a hydro run has existing `return_period_result` rows
- **WHEN** the historical backfill helper refreshes quality
- **THEN** `result_rows`, `return_period_rows`, `warning_rows`, and max-window
  counters are populated as before
- **AND** explicit quality fields have deterministic defaults.

### Requirement: Flood quality storage absence does not fail q_down

Missing or not-yet-migrated flood quality storage SHALL fail closed for flood
products without marking q_down/discharge product readiness unavailable.

#### Scenario: quality table unavailable during read compatibility check

- **GIVEN** flood quality storage is absent or lacks the new explicit fields
- **WHEN** helper/read compatibility code checks run flood product quality
- **THEN** flood return-period quality is unavailable or degraded with a clear
  reason
- **AND** q_down/discharge readiness is not marked unavailable solely because
  flood quality storage is missing.

### Requirement: No null-result index amplification

The migration path for this change SHALL NOT create new NULL partial indexes on
`flood.return_period_result`.

#### Scenario: migration applied to an empty database

- **WHEN** migrations are applied
- **THEN** `flood.run_product_quality` has the explicit quality columns
- **AND** indexes named `return_period_result_null_return_period_run_idx` and
  `return_period_result_null_warning_level_run_idx` are absent unless they
  already existed before this change.

### Requirement: Explicit quality survives empty source refresh

Refreshing quality from source rows SHALL NOT delete an explicit unavailable
quality row merely because source result rows are absent.

#### Scenario: explicit unavailable row and no source rows

- **GIVEN** `flood.run_product_quality` has an explicit unavailable row
- **AND** `flood.return_period_result` has no rows for the run
- **WHEN** a single-run quality refresh is requested
- **THEN** the explicit quality row remains available for downstream readers.

### Requirement: Empty no-curve return-period rows are not stored

The return-period worker SHALL NOT store `flood.return_period_result` rows that
have no usable flood product because the frequency curve is missing or unusable.

#### Scenario: all segments lack usable frequency curves

- **GIVEN** a run has q_down peak and timestep values
- **AND** no segment has a usable frequency curve
- **WHEN** return periods are computed
- **THEN** `flood.return_period_result` contains no rows for that run/network
  whose `quality_flag` is `no_frequency_curve` or `no_usable_frequency_curve`
- **AND** the run has an explicit `flood.run_product_quality` row
- **AND** the quality row is not `ready`
- **AND** expected coverage and no-curve counters describe the skipped
  evaluations.

#### Scenario: partial curve coverage

- **GIVEN** a run has q_down values for segments with and without usable
  frequency curves
- **WHEN** return periods are computed
- **THEN** rows are stored only for evaluations with usable flood product data
- **AND** skipped no-curve evaluations are represented in
  `flood.run_product_quality`
- **AND** the run quality is not `ready`.

### Requirement: Recomputes clear stale return-period rows before writing

The return-period worker SHALL clear the current run/network's previous
return-period result rows before writing replacement peak/timestep rows,
including recomputes whose replacement row batch is empty.

#### Scenario: stale no-curve rows exist and recompute writes no rows

- **GIVEN** `flood.return_period_result` contains stale null no-curve rows for a
  run/network
- **AND** the current recomputation has no usable frequency curves
- **WHEN** return periods are computed
- **THEN** the stale rows for that run/network are removed
- **AND** no replacement empty rows are written
- **AND** explicit quality remains available for downstream readers.

### Requirement: Meaningful usable-curve rows continue to be stored

The worker SHALL continue storing rows that contain usable return-period product
data.

#### Scenario: usable curves with warning thresholds unavailable

- **GIVEN** usable frequency curves exist
- **AND** warning thresholds are unavailable by quality contract
- **WHEN** return periods are computed
- **THEN** rows with non-null `return_period` are stored
- **AND** `warning_level` may remain null
- **AND** explicit run quality identifies warning-threshold unavailability.

#### Scenario: complete usable curves

- **GIVEN** all segments have usable frequency curves and warning thresholds
  are available
- **WHEN** return periods are computed
- **THEN** peak and timestep rows are stored as before
- **AND** run quality can be `ready`.

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

### Requirement: Historical No-Curve Cleanup Is Auditable And Safe By Default

The system SHALL provide an operator-facing cleanup command for historical
`flood.return_period_result` rows where `return_period IS NULL`,
`warning_level IS NULL`, and `quality_flag` is `no_frequency_curve` or
`no_usable_frequency_curve`.

#### Scenario: Dry-run manifest does not mutate database

- **WHEN** the cleanup command is run without explicit apply mode
- **THEN** it SHALL produce a manifest with candidate counts and affected runs
- **AND** it SHALL delete zero rows

#### Scenario: Apply deletes only preserved-quality no-curve candidates

- **WHEN** apply mode is enabled with a bounded batch size
- **AND** every affected run has explicit `flood.run_product_quality`
- **THEN** each batch SHALL delete only rows matching the no-curve null predicate
- **AND** deletion SHALL recheck the same filters and candidate predicate used
  by dry-run summaries
- **AND** batch ordering and resume evidence SHALL use the stable row identity
  tuple `(run_id, river_network_version_id, river_segment_id, duration,
  valid_time, max_over_window)`
- **AND** rows with non-null `return_period` or non-null `warning_level` SHALL
  remain
- **AND** affected run quality summaries SHALL remain present after cleanup

#### Scenario: Missing explicit quality blocks apply

- **WHEN** candidate rows exist for a run without `flood.run_product_quality`
- **THEN** apply mode SHALL fail before deletion
- **AND** the manifest or error SHALL identify the missing quality run
- **AND** no force or override option SHALL allow deletion for that run

#### Scenario: Filters define one candidate set

- **WHEN** the cleanup command is run with any combination of `run_id`,
  `basin_version_id`, `source_id`, or `cycle_time` range filters
- **THEN** dry-run counts, missing-quality checks, batch identity selection,
  deletion, and manifest affected-run lists SHALL all use the same filtered
  candidate set

### Requirement: Historical No-Curve Cleanup Supports Bounded Resume Evidence

The cleanup command SHALL execute destructive cleanup in bounded batches and
record enough evidence to resume or audit partial completion.

#### Scenario: Batch manifest records committed progress

- **WHEN** apply mode deletes candidate rows in multiple batches
- **THEN** the manifest SHALL include per-batch deleted row counts, duration,
  status, and a cursor or continuation hint
- **AND** the continuation hint SHALL be based on the stable row identity tuple,
  not offset pagination

#### Scenario: Timescale metadata absence is non-fatal

- **WHEN** Timescale chunk metadata is unavailable
- **THEN** dry-run manifest generation SHALL still succeed
- **AND** it SHALL record chunk distribution as unavailable while retaining time
  bucket distribution

#### Scenario: Existing production artifacts are out of scope

- **WHEN** cleanup runs in dry-run or apply mode
- **THEN** it SHALL NOT delete `hydro.river_timeseries` rows or object-store
  `/runs` artifacts
- **AND** it SHALL NOT perform schema or index maintenance such as `DROP INDEX`,
  `REINDEX`, or `VACUUM FULL`

