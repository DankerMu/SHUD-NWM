# Spec Delta: cross-cycle-warm-start-chaining

## MODIFIED Requirements

### Requirement: Forecast checkpoint mechanics are functional, not assumed
The forecast long-run checkpoint path SHALL be made end-to-end functional: checkpoint capture,
normalization, consume-side filename, and header-time validation must all hold. Because in-flight
capture samples an in-place-rewritten `*.cfg.ic.update`, capture SHALL NOT depend on solve speed:
when the sampling watcher misses a requested hour on a run the solver completed successfully, the
runtime SHALL deterministically recover that checkpoint in-process — by re-running SHUD from the
same staged IC/forcing with the end time shortened to the missed hour into a scratch directory —
before the run is allowed to fail, and a recovered checkpoint SHALL pass the same header-time and
structural-completeness gates as a watcher capture.

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
- **AND** the runtime's own in-process recovery rerun after a watcher miss is not a scheduled
  production run: it executes inside the same runtime invocation and Slurm task, writes only to a
  scratch directory under the run workspace, and creates no scheduler-visible run, candidate, or
  journal entry
- **AND** explicit short reruns remain allowed **only** as manual repair for already completed
  historical cycles that missed checkpoint capture (the in-process recovery rerun above is the
  sole automated exception, and it is not a scheduled production run).

#### Scenario: Watcher miss on a successful solve recovers deterministically
- **WHEN** the solver exits successfully but a requested checkpoint hour was never observed by the
  sampling watcher (e.g. the solve was fast enough that each intermediate header state was alive
  for less than the poll interval)
- **THEN** the runtime re-runs SHUD from the same staged IC and forcing with the end time
  shortened to exactly the missed hour, into a scratch output directory that contains no content
  from any earlier attempt, with the main solve and all recovery reruns together bounded by ONE
  shared runtime-timeout budget (an hour with no remaining budget is skipped and recorded)
- **AND** the rerun's final `*.cfg.ic.update` is installed as the checkpoint only if its header
  minute matches the requested hour and its body passes structural completeness for the expected
  river count — the same gates as a watcher capture
- **AND** the installed entry records `provenance` = `post_run_recovery`, and the run's staged
  SHUD config is byte-identical after recovery to its pre-recovery content

#### Scenario: A miss is a hard failure only after recovery also fails
- **WHEN** the recovery rerun fails (non-zero exit, timeout, or a produced state that fails the
  header or structural gates)
- **THEN** the gate-failing candidate is discarded (not installed, no file left under
  `state_checkpoints/`), and the run fails with the stable error code
  `STATE_CHECKPOINTS_MISSING`
- **AND** the `state_checkpoints.json` manifest is written whenever checkpoint hours were
  requested — including when nothing was captured — and both the failure message and the
  manifest include the trail of distinct `*.cfg.ic.update` header minutes the watcher observed
  plus a per-hour recovery-outcome trail (`recovery_outcomes`) naming how each missing hour's
  recovery ended, so the miss is locatable from evidence alone

#### Scenario: Checkpoint manifest consumers tolerate the diagnostic fields
- **WHEN** an existing consumer reads `state_checkpoints.json` (state save/QC via
  `_load_state_checkpoint_manifest`)
- **THEN** checkpoint entries parse exactly as before: the added top-level
  `observed_header_minutes` and `recovery_outcomes` keys and the per-entry `provenance` key are
  ignored by consumers that do not use them, and no entry field consumed today changes shape or
  meaning
