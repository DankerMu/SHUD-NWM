## MODIFIED Requirements

### Requirement: Forecast checkpoint mechanics are functional, not assumed
The forecast long-run checkpoint path SHALL be made end-to-end functional: checkpoint capture,
normalization, consume-side filename, and header-time validation must all hold. Because in-flight
capture samples an in-place-rewritten `*.cfg.ic.update`, capture SHALL NOT depend on solve speed:
when the sampling watcher misses a requested hour on a run the solver completed successfully, the
runtime SHALL deterministically recover that checkpoint in-process — by re-running SHUD from the
same staged IC/forcing with the end time shortened to the missed hour into a scratch directory —
before the run is allowed to fail, and a recovered checkpoint SHALL pass the same header-time and
structural-completeness gates as a watcher capture. The `state_checkpoints.json` manifest SHALL be
written after every successful solve — checkpoint hours requested or not (a zero-checkpoint-hour
run writes an empty `checkpoints` list rather than omitting the file) — and SHALL carry a
top-level `provenance` block (`run_id`, `generated_at` UTC wall-clock at manifest write,
`slurm_job_id`, `array_task_id`, and `requested_checkpoint_hours` — the hour list the run was
asked to capture) identifying the execution that produced it, plus a top-level `final_ic` entry
(relative path, original SHUD filename, and content checksum). The final IC is identified by
exactly two candidate paths — the tracker's watched `output_dir/<project_name>.cfg.ic.update`,
else `output_dir/<project_name>.cfg.ic` — with no recursive search and no other filename ever
recorded; when neither exists, no `final_ic` entry is written. The solver failure lanes
(spawn failure, timeout, nonzero exit) SHALL NOT write the manifest: manifest presence is a
witness that an attempt of this run completed its solve successfully in this output tree, and
every artifact the manifest names is integrity-pinned by its checksum.

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
  production run: it executes inside the same runtime invocation and Slurm task, confines the
  rerun solver's output to a scratch directory under the run workspace (while additionally
  writing per-hour recovery logs into the run log directory, temporarily rewriting the run's
  staged cfg and restoring it best-effort — a restore-write failure is recorded as that hour's
  `cfg_restore_failed` outcome, never masking the hour's own result — and installing an
  accepted checkpoint into `output/state_checkpoints/`), and creates no scheduler-visible run,
  candidate, or journal entry
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
  SHUD config is byte-identical after recovery to its pre-recovery content (the restore is
  best-effort: on a restore-write failure the run records `cfg_restore_failed` for that hour
  instead of failing, and the workspace cfg may retain the shortened horizon until the next
  `execute()` re-templates it)

#### Scenario: A miss is a hard failure only after recovery also fails
- **WHEN** the recovery rerun fails (non-zero exit, timeout, or a produced state that fails the
  header or structural gates)
- **THEN** the gate-failing candidate is discarded (not installed, no file left under
  `state_checkpoints/`), and the run fails with the stable error code
  `STATE_CHECKPOINTS_MISSING`
- **AND** the `state_checkpoints.json` manifest is written (this lane sits after a successful
  solve, so the manifest-presence witness holds) — including when nothing was captured — and
  both the failure message and the manifest include the trail of distinct `*.cfg.ic.update`
  header minutes the watcher observed plus a per-hour recovery-outcome trail
  (`recovery_outcomes`) naming how each missing hour's recovery ended, so the miss is locatable
  from evidence alone

#### Scenario: Checkpoint manifest consumers tolerate the diagnostic fields
- **WHEN** an existing consumer reads `state_checkpoints.json` (state save/QC via
  `_load_state_checkpoint_manifest`)
- **THEN** checkpoint entries parse exactly as before: the added top-level
  `observed_header_minutes`, `recovery_outcomes`, `provenance`, and `final_ic` keys and the
  per-entry `provenance` key are ignored by consumers that do not use them, and no entry field
  consumed today changes shape or meaning

#### Scenario: Manifest presence is a solver-success witness
- **WHEN** a run's solver fails to spawn, times out, or exits nonzero
- **THEN** no `state_checkpoints.json` is written for that attempt, so a later state-save
  admission check can treat manifest presence in an output tree as evidence that an attempt of
  this run completed its solve successfully there
- **AND** a successful solve with zero requested checkpoint hours writes the manifest with an
  empty `checkpoints` list, `provenance.requested_checkpoint_hours: []`, and a `final_ic` entry
  naming and checksumming the solve's final state, rather than omitting the file.

#### Scenario: Failure-message diagnostics survive receipt truncation and render minutes losslessly

- **WHEN** the `STATE_CHECKPOINTS_MISSING` failure message is truncated to the task-outcome
  receipt's message budget while the observed-header-minutes trail is long (its length grows
  with run duration), or an observed header minute is in epoch-minutes form
- **THEN** the per-hour recovery-outcome trail is placed before the observed-header-minutes
  trail in the message, so truncation sacrifices the observed trail first and the receipt's
  `error_message` still names how each missing hour's recovery ended
- **AND** header minutes rendered into the failure message — both the observed trail and the
  recovery-outcome `gate_rejected(header=...)` value — render in plain decimal form (never
  scientific notation), so an epoch-form minute remains greppable against the manifest's
  full-precision values
