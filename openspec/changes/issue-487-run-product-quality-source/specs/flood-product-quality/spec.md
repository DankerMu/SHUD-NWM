# flood-product-quality Spec Delta

## ADDED Requirements

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
