# strict-warm-start Specification

## Purpose
TBD - created by archiving change issue-496-strict-warm-start. Update Purpose after archive.
## Requirements
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

#### Scenario: Newly registered model may seed its first cycle cold

- **GIVEN** generation-scoped state history proves the model has no prior state in any generation
- **AND** the scheduler records `cold_new_model` with `cold_start_reason=no_prior_history`
- **WHEN** the model's first forecast cycle is staged
- **THEN** that first cycle may use cold start with `init_mode=1`
- **AND** after it produces a successor checkpoint, the next cycle is governed by exact-successor
  strict warm-start rules and may not cold-start again

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

#### Scenario: Selected exact state becomes unavailable during runtime staging

- **GIVEN** strict mode selected an exact successor and the runtime manifest records
  `warm_start_policy=exact_required`
- **WHEN** the state object is missing, unreadable, or has a checksum mismatch during staging
- **THEN** runtime fails with a stable warm-state-unavailable error
- **AND** it does not query an older state and does not change the manifest to cold start

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

### Requirement: Terminal init-state comparison SHALL distinguish absence from conflict via a single shared helper

The scheduler SHALL evaluate a terminal decision's recorded init-state identity against the strict warm-start resolution through one shared helper returning exactly one of `match`, `absent`, `conflict`. Comparison SHALL be per-present-field: `absent` means the terminal evidence carries no init-state identity fields at all; when `init_state_id` is present, every additionally present field (`checksum`, `uri`, `valid_time`; redaction placeholders skipped as today) SHALL agree for `match`; any present field in disagreement SHALL classify as `conflict`. A record carrying only `init_state_id` SHALL retain today's match semantics unchanged; a record carrying other identity fields without `init_state_id` SHALL classify as `conflict`. The helper SHALL be a pure field comparison: the candidate path's existing special branches — the `candidate_state` terminal-source branch and the `COLD_START_QUARANTINED` escape — SHALL remain candidate-side short-circuits ahead of the helper and SHALL NOT enter the verdict path; when the strict resolution carries no `candidate_state`, the verdict path SHALL bypass the helper and keep today's gap behavior. The verdict path SHALL consume this helper for the init-identity field comparison; the candidate path's final `hydro_run`-record comparison SHALL retain today's selected-driven strict semantics (`_warm_state_record_matches`: a field present on the selected state but absent on the observed record is a mismatch) byte-identical — the observed-driven helper semantics would silently reclassify legacy id-only records from mismatch to match and reroute the budgeted strict-warm-start mismatch decision onto the unbudgeted run-manifest-missing path. The candidate path's remaining admission segments are out of scope.

#### Scenario: Absence is distinguished from conflict

- **WHEN** a terminal-success row carries no init-state identity fields
- **THEN** the helper returns `absent`, not `conflict`

#### Scenario: Legacy id-only records keep matching

- **WHEN** a terminal row carries only `init_state_id` and it equals the strict resolution's state id
- **THEN** the helper returns `match`, byte-identical in effect to today's verdict-side behavior

#### Scenario: Candidate-side budget routing is preserved for id-only records

- **WHEN** the candidate admission ladder compares a strict `candidate_state` carrying a checksum against a terminal `hydro_run` record carrying only a matching `init_state_id`
- **THEN** the candidate wrapper classifies it as not-match exactly as today, routing the decision through the budgeted `strict_warm_start_terminal_init_state_mismatch` path — never through the unbudgeted run-manifest gate

#### Scenario: Any present-field disagreement is conflict

- **WHEN** a terminal row carries `init_state_id` plus at least one further identity field and any present field disagrees with the strict resolution
- **THEN** the helper returns `conflict`

#### Scenario: Candidate-path special branches stay candidate-side

- **WHEN** the candidate ladder evaluates a terminal whose evidence takes the `candidate_state` terminal-source branch or the `COLD_START_QUARANTINED` escape
- **THEN** the candidate-side wrapper short-circuits to match ahead of the helper, the emitted candidate decision shapes are unchanged from today, and the cycle-completion verdict path never inherits these escapes — it classifies the same shapes through the plain helper (`absent`), so their completion still requires proven successor continuity (the design's named cold-seed residual risk)

#### Scenario: Strict resolution without candidate_state keeps gap

- **WHEN** the strict warm-start resolution is ready but carries no `candidate_state` (cold-start generation shapes)
- **THEN** the verdict path bypasses the helper and the cycle verdict remains `gap` as today

