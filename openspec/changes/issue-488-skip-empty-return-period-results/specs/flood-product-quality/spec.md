# flood-product-quality Spec Delta

## ADDED Requirements

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
