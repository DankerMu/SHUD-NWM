## ADDED Requirements

### Requirement: Warm-start IC is produced at the next cycle's init time
A production cycle SHALL produce, via a short analysis/nowcast segment `[T_N, T_{N+1}]` whose
`hydro_run.end_time == T_{N+1}`, a SHUD initial-condition snapshot valid at the next cycle's init
time, closing the gap that a forecast run only saves state at its forecast-window `end_time`.
(SHUD's single overwritten `*.cfg.ic.update` cannot reliably yield an interim `T_{N+1}` state, so
the restart-cadence path is not used unless timestamped non-overwriting restart artifacts are added.)

#### Scenario: Saved snapshot is keyed at the next cycle init time
- **WHEN** the analysis segment for successor cycle N+1 completes with `end_time == T_{N+1}`
- **THEN** the saved snapshot has `valid_time == T_{N+1}` and is normalized to a canonical
  `state.cfg.ic` recording the original SHUD filename
- **AND** it is not keyed at any forecast-window `end_time`.

#### Scenario: Three-way time consistency
- **WHEN** cycle N+1 consumes the saved state
- **THEN** the snapshot `valid_time`, the `.cfg.ic` header minute-time, and the run's
  `start_time`/`cycle_time` all equal `T_{N+1}`
- **AND** a mismatch among the three is a recorded blocker, not a silent restart at the wrong time.

### Requirement: Analysis-segment production mechanics are functional, not assumed
The analysis segment SHALL be made end-to-end functional (the time semantics alone are not enough):
final-state normalization, restart cadence, consume-side filename, and causal forcing must all hold.

#### Scenario: Restart cadence lands on the next cycle init time
- **WHEN** the analysis segment runs `[T_N, T_{N+1}]`
- **THEN** `Update_IC_STEP` is set to a cadence that writes a restart state exactly at `T_{N+1}`
  (the default 1440-minute cadence is not assumed; short 6h/12h cycles must still land)
- **AND** the saved state is the state at `T_{N+1}`, not an earlier modulo boundary.

#### Scenario: Final-state normalization and consume-side filename
- **WHEN** native SHUD writes its end state to `*.cfg.ic.update`
- **THEN** it is normalized to the canonical `state.cfg.ic` object before save, and the consuming
  run materializes/renames it to `<project_name>.cfg.ic` that SHUD actually reads
- **AND** the original SHUD filename and target `valid_time` are recorded.

#### Scenario: Causal forcing for the analysis segment
- **WHEN** the daemon runs in real time and builds the `[T_N, T_{N+1}]` analysis forcing
- **THEN** it uses a causal, no-future-leak source (e.g. cycle N's `0..Δ` forecast lead or
  best-available nowcast), not whole-day ERA5 truncated to 00Z
- **AND** ERA5 is used only in an explicit delayed-reanalysis mode with recorded latency.

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
- **AND** the cycle falls back to the next usable state or a recorded cold start, never failing the
  cycle solely for a missing successor state.

#### Scenario: State-variable QC
- **WHEN** a snapshot is QC'd before becoming usable
- **THEN** row counts match mesh/river/lake counts, values pass range/non-negative checks for
  canopy/snow/surface/unsat/groundwater/river-stage (and lake-stage if present), and the restart
  first-step water-balance delta is within threshold for soil moisture, groundwater, and channel
  storage
- **AND** a failing check marks the snapshot unusable with a recorded reason.

### Requirement: Warm-start quality uses the canonical enum
Recorded warm-start quality SHALL use the existing canonical values, not a new third set.

#### Scenario: Canonical quality values
- **WHEN** quality is recorded in run/cycle evidence
- **THEN** it is one of `fresh`, `degraded_stale_init_state`, `cold_start_no_state`, or
  `cold_start_stale_state` (an aggregate `cold_start` display value MAY be derived, but the receipt
  retains the specific underlying value).
