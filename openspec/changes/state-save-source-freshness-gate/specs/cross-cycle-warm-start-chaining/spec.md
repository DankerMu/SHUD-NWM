# cross-cycle-warm-start-chaining (delta)

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

## ADDED Requirements

### Requirement: Publish-side state admission is fail-closed
`save_state_for_run` SHALL verify, before selecting any state artifact, that the source output
tree was produced by a successful solve of the run named in the caller's context, and SHALL
publish only artifacts the manifest names and checksums: an output root must exist; a
`state_checkpoints.json` manifest must be present at
`<root>/state_checkpoints/state_checkpoints.json` with a `provenance` block whose `run_id`
matches the context; and every manifest-declared artifact (checkpoint entries and, on the
fallback lane, the `final_ic` entry) must be present, carry a declared checksum, and match it —
a declared entry without a checksum is itself a violation. Each violation SHALL reject the
publish with a typed reason (`STATE_SAVE_SOURCE_OUTPUT_MISSING`,
`STATE_SAVE_SOURCE_MANIFEST_MISSING`, `STATE_SAVE_SOURCE_PROVENANCE_MISSING`,
`STATE_SAVE_SOURCE_PROVENANCE_MISMATCH`, `STATE_SAVE_SOURCE_MANIFEST_INCOMPLETE`,
`STATE_SAVE_SOURCE_ARTIFACT_CHECKSUM_MISMATCH`, `STATE_SAVE_SOURCE_CHECKPOINTS_UNCAPTURED`,
`STATE_SAVE_SOURCE_FINAL_IC_MISSING`)
surfaced through a `StateManagerError` and a nonzero CLI exit, with no snapshot write, no QC
record, and no state index mutation. When multiple output roots exist, the first root passing
the full check is the verified root; a root failing the check falls through to the next, except
a present-but-unparseable manifest or an unsafe declared path, which keep the existing
`Invalid state checkpoint manifest` / `State checkpoint path is unsafe` /
`State checkpoint path escapes output directory` hard errors unchanged. The final-IC fallback
SHALL be reachable only when a verified manifest declares zero checkpoints AND
`provenance.requested_checkpoint_hours` is empty, and SHALL publish only the file the
manifest's `final_ic` entry names, checksum-verified — an undeclared file is never selected,
and a fallback-lane manifest without a `final_ic` entry rejects with
`STATE_SAVE_SOURCE_FINAL_IC_MISSING`. A
verified manifest with requested hours but zero captured checkpoints rejects with
`STATE_SAVE_SOURCE_CHECKPOINTS_UNCAPTURED`. The admission contract is identity plus
solver-success witness plus named-artifact integrity — deliberately not wall-clock recency, so
a retry that legitimately reuses durable output from the original successful solve still
publishes.

#### Scenario: Missing output tree rejects the publish
- **WHEN** `save_state_for_run` runs for a run whose output root exists in no probed location
  (workspace or `output_uri`)
- **THEN** the publish is rejected with `STATE_SAVE_SOURCE_OUTPUT_MISSING` and no
  `save_state_snapshot`, QC, or index call is made.

#### Scenario: Failed-attempt residue rejects the publish
- **WHEN** the output root exists and contains `*.cfg.ic`/`*.cfg.ic.update` files but no
  `state_checkpoints.json` (the residue of a killed or failed solve, or a pre-upgrade tree)
- **THEN** the publish is rejected with `STATE_SAVE_SOURCE_MANIFEST_MISSING` — no rglob
  fallback runs and nothing is stamped `valid_time = run.end_time`
- **AND** the recovery path is re-running the forecast, which regenerates a witness-bearing
  tree.

#### Scenario: Manifest-declared checkpoints must all be present and intact
- **WHEN** the manifest declares N checkpoint entries and fewer than N referenced files exist,
  or a declared entry carries no `checksum`, or an entry is malformed (not a dict, missing
  `relative_path`/`valid_time`, or referencing a non-regular file), or the `checkpoints` value
  itself is not a list, or a referenced file's content no longer matches its declared
  `checksum`
- **THEN** the publish is rejected with `STATE_SAVE_SOURCE_MANIFEST_INCOMPLETE` (missing or
  malformed declarations — the declared set is the raw `checkpoints` array, never the loader's
  silently-filtered view) respectively `STATE_SAVE_SOURCE_ARTIFACT_CHECKSUM_MISMATCH` (content
  drift) naming the offending entries, instead of silently publishing the surviving subset.

#### Scenario: Foreign or provenance-less manifest rejects the publish
- **WHEN** the manifest lacks a `provenance` block, or its `provenance.run_id` names a
  different run than the caller's context
- **THEN** the publish is rejected with `STATE_SAVE_SOURCE_PROVENANCE_MISSING` (respectively
  `STATE_SAVE_SOURCE_PROVENANCE_MISMATCH`).

#### Scenario: Zero-checkpoint runs stay publishable through the gated fallback
- **WHEN** a verified manifest declares an empty `checkpoints` list with
  `provenance.requested_checkpoint_hours: []` and its `final_ic` entry's file is present and
  checksum-matched in the verified root
- **THEN** the publish proceeds via the final-IC fallback with `valid_time == run.end_time`,
  publishing exactly the manifest-named file — a stale or foreign IC lying in the tree is never
  selected because it is not the named, checksummed artifact
- **AND** the same manifest without a `final_ic` entry rejects with
  `STATE_SAVE_SOURCE_FINAL_IC_MISSING` instead of searching the tree.

#### Scenario: A total checkpoint miss cannot downgrade to the fallback
- **WHEN** a verified manifest records non-empty `provenance.requested_checkpoint_hours` but an
  empty `checkpoints` list (the `STATE_CHECKPOINTS_MISSING` tree retried into state save)
- **THEN** the publish is rejected with `STATE_SAVE_SOURCE_CHECKPOINTS_UNCAPTURED` instead of
  silently publishing the final IC in place of the requested checkpoint states.

#### Scenario: Durable-output-reuse retries keep publishing
- **WHEN** a retry restarts a candidate at the state-save stage reusing the durable output of
  the original successful solve (`durable_shud_output_reused`), and the witness-bearing tree is
  intact
- **THEN** the publish succeeds — the admission contract imposes no wall-clock recency
  predicate that would reject artifacts older than the retry submission
- **AND** if that tree has since been removed, the publish rejects with the applicable typed
  reason instead of downgrading to a weaker source.

#### Scenario: Rejection surfaces a typed reason through the CLI exit
- **WHEN** any admission predicate rejects
- **THEN** the typed reason token leads the `StateManagerError` message, the CLI exits nonzero
  with the token on stderr, and the orchestrator's existing stage classification records the
  failed `state_save_qc` stage — the reject is never a silent downgrade to a weaker source.
