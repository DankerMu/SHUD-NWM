## ADDED Requirements

### Requirement: Strict Forecast Warm-Start Mode

The orchestrator SHALL support an explicit strict forecast warm-start mode for
business production forecast runs.

#### Scenario: Production env enables strict mode

- **GIVEN** `NHMS_REQUIRE_FORECAST_WARM_START=true`
- **WHEN** orchestrator config is constructed from the production environment
- **THEN** strict forecast warm-start mode is enabled
- **AND** forecast staging must enforce exact-successor state validation

#### Scenario: Non-strict compatibility remains available

- **GIVEN** strict forecast warm-start mode is disabled
- **WHEN** a forecast or analysis path has no exact successor state
- **THEN** existing non-strict fallback or cold-start behavior remains available
- **AND** legacy non-production tests can opt out of strict mode

### Requirement: Strict Mode Requires Exact Successor State

In strict mode, a forecast run SHALL use only an exact successor state snapshot
whose `valid_time` equals the target `cycle_time`.

#### Scenario: Exact successor exists for 00 to 12

- **GIVEN** strict mode is enabled
- **AND** the target forecast cycle is UTC `12`
- **AND** an exact state snapshot exists with `valid_time == cycle_time`
- **AND** the state has `lead_hours == 12`
- **AND** the state is usable, QC-passing, and lineage-compatible
- **WHEN** the forecast is staged
- **THEN** that state is selected as the initial state
- **AND** the run manifest records `initial_state.valid_time == cycle_time`
- **AND** runtime `init_mode` indicates warm start

#### Scenario: Exact successor exists for 12 to next-day 00

- **GIVEN** strict mode is enabled
- **AND** the target forecast cycle is UTC `00`
- **AND** an exact state snapshot exists with `valid_time == cycle_time`
- **AND** the state has `lead_hours == 12`
- **WHEN** the forecast is staged
- **THEN** that state is selected as the initial state
- **AND** no older latest-usable state is considered

#### Scenario: Exact successor is missing

- **GIVEN** strict mode is enabled
- **AND** no exact state snapshot exists for `valid_time == cycle_time`
- **WHEN** the forecast is requested
- **THEN** the orchestrator returns a stable missing-successor error
- **AND** the error code is `warm_start_successor_checkpoint_missing`
- **AND** it does not call latest-usable fallback
- **AND** it does not write a run manifest, create or update a hydro_run, or
  submit Slurm work

### Requirement: Strict Mode Rejects Invalid Successor States

In strict mode, an exact successor state SHALL be rejected when it is unusable,
QC-failing, lineage-incompatible, or not the previous allowed-cycle `+12h`
checkpoint.

#### Scenario: Exact state is unusable or fails QC

- **GIVEN** strict mode is enabled
- **AND** an exact state exists for the target cycle
- **AND** the state has `usable_flag=false` or fails the state-variable QC hook
- **WHEN** the forecast is requested
- **THEN** the orchestrator returns a stable unusable/QC error
- **AND** the error code is `warm_start_successor_checkpoint_unusable`
- **AND** no run manifest, hydro_run, or Slurm side effect is produced

#### Scenario: Exact state lineage does not match target

- **GIVEN** strict mode is enabled
- **AND** an exact state exists for the target cycle
- **AND** the state source, model package version, or model package checksum does
  not match the target forecast
- **WHEN** the forecast is requested
- **THEN** the orchestrator returns a stable lineage mismatch error
- **AND** the error code is `warm_start_lineage_mismatch`
- **AND** no run manifest, hydro_run, or Slurm side effect is produced

#### Scenario: Exact state is not the +12h successor checkpoint

- **GIVEN** strict mode is enabled
- **AND** an exact state exists for the target cycle
- **AND** the state `lead_hours` is not `12`
- **WHEN** the forecast is requested
- **THEN** the orchestrator returns a stable lineage mismatch error
- **AND** the error code is `warm_start_lineage_mismatch`
- **AND** the state is not used as a forecast initial state

### Requirement: Prefilled Warm-Start Fields Use Same Strict Validator

Scheduler-prefilled warm-start fields SHALL NOT bypass strict exact-successor
validation.

#### Scenario: Scheduler prefilled state is invalid

- **GIVEN** strict mode is enabled
- **AND** the scheduler provides `init_state_uri` or `init_state_id` on a basin
- **AND** the referenced or described state is not an exact, usable,
  QC-passing, lineage-compatible `lead_hours=12` successor
- **WHEN** `orchestrate_cycle` applies cohort warm-start fields
- **THEN** orchestration fails with the same stable strict warm-start error
- **AND** no cycle-stage manifest, run manifest, hydro_run, or Slurm side effect
  is produced

#### Scenario: Scheduler prefilled state is valid

- **GIVEN** strict mode is enabled
- **AND** the scheduler provides a valid exact successor state on a basin
- **WHEN** `orchestrate_cycle` applies cohort warm-start fields
- **THEN** the state remains selected
- **AND** the same state identity, checksum, valid time, and lineage flow into
  the scheduler basin record, cycle-stage entries, and runtime manifest
