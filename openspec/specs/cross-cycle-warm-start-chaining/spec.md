# cross-cycle-warm-start-chaining Specification

## Purpose
TBD - created by archiving change m24-multibasin-continuous-daemon-live. Update Purpose after archive.
## Requirements
### Requirement: Warm-start IC is produced at the next cycle's init time
A production forecast cycle SHALL run SHUD for the full product horizon and preserve selected
T+6/T+12 checkpoint states from the same long run, so successor cycles can initialize from a SHUD
initial-condition snapshot valid at their init time. `Update_IC_STEP` is a checkpoint write cadence,
not a shortened forecast horizon or a request for extra short production runs.

#### Scenario: Saved snapshot is keyed at the next cycle init time
- **WHEN** the forecast long run for cycle N writes a checkpoint whose header time equals `T_{N+1}`
- **THEN** the saved snapshot has `valid_time == T_{N+1}` and is normalized to a canonical
  `state.cfg.ic` recording the original SHUD filename
- **AND** the forecast run still has `end_time == T_N + forecast_horizon_hours`.

#### Scenario: Three-way time consistency
- **WHEN** cycle N+1 consumes the saved state
- **THEN** the snapshot `valid_time`, the `.cfg.ic` header minute-time, and the run's
  `start_time`/`cycle_time` all equal `T_{N+1}`
- **AND** a mismatch among the three is a recorded blocker, not a silent restart at the wrong time.

### Requirement: Forecast checkpoint mechanics are functional, not assumed
The forecast long-run checkpoint path SHALL be made end-to-end functional: checkpoint capture,
normalization, consume-side filename, and header-time validation must all hold.

#### Scenario: Restart cadence lands on the next cycle init time
- **WHEN** the forecast long run starts at `T_N`
- **THEN** `Update_IC_STEP` is set to a cadence that writes restart states at configured successor
  init offsets such as T+6 and T+12
  (the default 1440-minute cadence is not assumed; short 6h/12h cycles must still land)
- **AND** the saved state is the state at the target successor init time, not an earlier modulo
  boundary.
- **AND** the runtime manifest and SHUD config retain the full forecast horizon.

#### Scenario: Final-state normalization and consume-side filename
- **WHEN** native SHUD writes a checkpoint state to `*.cfg.ic.update`
- **THEN** it is normalized to the canonical `state.cfg.ic` object before save, and the consuming
  run materializes/renames it to `<project_name>.cfg.ic` that SHUD actually reads
- **AND** the original SHUD filename and target `valid_time` are recorded.

#### Scenario: Checkpoint capture does not create extra production SHUD runs
- **WHEN** the daemon runs in unattended production mode
- **THEN** T+6/T+12 state preservation is performed by the running forecast process and
  `state_save_qc`, not by scheduling separate short checkpoint forecast runs
- **AND** explicit short reruns are allowed only as manual repair for already completed historical
  cycles that missed checkpoint capture.

### Requirement: Next cycle consumes the prior cycle's saved state
A production forecast cycle SHALL initialize SHUD from the snapshot valid at its init time when one
exists, not from the packaged calibrated state.

#### Scenario: Two-cycle warm continuity (falsifiable)
- **WHEN** cycle N has no prior state (cold start) and saves a snapshot with `valid_time == T_{N+1}`,
  then cycle N+1 runs
- **THEN** cycle N+1's runtime manifest `initial_state.ic_file_uri`, checksum, and lineage equal
  that snapshot, with `init_mode=3`
- **AND** the packaged calibrated state is not used for cycle N+1.

#### Scenario: Cohort manifests agree on the selected state
- **WHEN** a cohort forecast cycle prepares manifests
- **THEN** the scheduler basin record, the cycle-stage manifest, and the forecast runtime manifest
  carry the same `init_state_uri` and checksum.

### Requirement: Warm-start selection enforces lineage and state integrity
Warm-start selection SHALL check producing source/cycle/lead, model package version, and checksum
lineage (beyond `valid_time` alone) and validate SHUD state-variable integrity before use.

#### Scenario: Reject incompatible-lineage state
- **WHEN** the candidate state was produced by a different model package version, a different
  source, or a lead beyond the configured `max_lead` policy
- **THEN** it is rejected with a stable rejection code recorded in evidence
- **AND** strict business-production mode keeps the candidate blocked for retry and does not select
  an older state or cold start; non-strict compatibility paths may retain their documented fallback.

#### Scenario: State-variable QC
- **WHEN** a snapshot is QC'd before becoming usable
- **THEN** row counts match mesh/river/lake counts, values pass range/non-negative checks for
  canopy/snow/surface/unsat/groundwater/river-stage (and lake-stage if present), and the restart
  first-step water-balance delta is within threshold for soil moisture, groundwater, and channel
  storage
- **AND** a failing check marks the snapshot unusable with a recorded reason.

#### Scenario: Bounded negative Unsat residual is projected to the physical floor
- **WHEN** SHUD serializes negative `Unsat` ODE residuals no deeper than 0.02 m per mesh row and
  their domain-row mean correction does not exceed 0.0002 m
- **THEN** state-save and warm-state consumption project the accepted values to exact zero
- **AND** evidence records the corrected value count, affected row count/fraction, maximum
  correction, and domain-row mean correction
- **AND** a deeper per-row correction or excessive domain-mean correction is rejected rather than
  hidden by normalization.

### Requirement: Warm-start quality uses the canonical enum
Recorded warm-start quality SHALL use the existing canonical values, not a new third set.

#### Scenario: Canonical quality values
- **WHEN** quality is recorded in run/cycle evidence
- **THEN** it is one of `fresh`, `degraded_stale_init_state`, `cold_start_no_state`, or
  `cold_start_stale_state` (an aggregate `cold_start` display value MAY be derived, but the receipt
  retains the specific underlying value).
