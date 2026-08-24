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
- **AND** a mismatch among the three is a blocker at the **candidate-snapshot** level — the
  drifted candidate is rejected and is never consumed at the wrong time; on the degradation
  terminals (next usable state, labeled cold start) the rejection is recorded through the
  corrupted-state channel, while the systemic-escalation and exact-warm-start terminals fail the
  run loudly without persisting the mark (escalation deliberately keeps the snapshots usable so
  the alarm repeats). The run-level terminal is governed by the forecast-warm-start
  degradation-ladder requirement, so a rejected candidate no longer implies a whole-run failure
  outside the exact-warm-start policy.

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
- **WHEN** SHUD serializes negative `Unsat` ODE residuals no deeper than 0.05 m per mesh row and
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

### Requirement: Cycle completion verdict SHALL tolerate init-state record absence when successor states prove continuity

The cycle completion verdict SHALL evaluate the candidate's terminal decision BEFORE any
strict-warm-start or successor admission early-return. Warm-start admission answers whether a
run SHOULD BE STARTED; it SHALL NOT veto the finding that a run ALREADY COMPLETED. When the
terminal decision is not a terminal success, the verdict SHALL fall back to today's
strict/successor gating unchanged.

When a terminal-success candidate's strict warm-start resolution is ready and names a state, the
completion verdict SHALL compare that selection against the journal-recorded identity returned by
the repository's optional completed-pipeline full-identity accessor whenever that accessor returns
a mapping; when the accessor is unavailable or returns no mapping, the verdict SHALL retain its
existing terminal `hydro_run` evidence input. The journal accessor SHALL expose the accepted-submit
cohort's recorded per-model terminal identity without widening bounded candidate evidence. If the
shared init-state comparison returns `conflict`, the verdict SHALL remain `gap` even when the
successor state is ready. If it returns `match`, the verdict SHALL be `complete` without requiring
successor evidence. If it returns `absent`, the verdict SHALL be `complete` only when the successor
state is ready (present in the state snapshot index and usable); when successor state is not ready,
not usable, or not evaluated, the verdict SHALL remain `gap`. Legacy per-basin terminal rows,
repositories without the optional accessor, and pre-change cohort rows with no recorded identity
SHALL keep these existing match/absence semantics unchanged.

When the strict warm-start resolution is NOT ready **for a reason that means no predecessor state
exists**, the comparison SHALL return `unverifiable` rather than `conflict`, and the verdict SHALL
be `complete` if and only if the successor state proves continuity — the same physical continuity
standard the `absent` branch already applies. This readiness/reason classification SHALL happen
without consulting the optional full-identity accessor: no strict selection exists to compare, and
an identity-provider failure SHALL NOT veto the terminal-first `unverifiable` verdict.

The not-ready reason SHALL be matched against a closed ALLOWLIST, never a denylist: a reason absent
from the allowlist SHALL classify as `conflict` and keep today's hard gap. The allowlist SHALL
contain only reasons whose meaning is "there is genuinely no predecessor state here", and SHALL
NOT contain any reason that reports something wrong with a state that does exist — a lineage or
checksum mismatch, an unusable or unreadable checkpoint, a missing or unavailable index, or
`state_snapshot_index_prior_checkpoint_missing_after_history` (which means history exists but its
checkpoint is gone: the operator-backfill population, an anomaly rather than an absence). A newly
introduced not-ready reason SHALL therefore default to `conflict` until it is deliberately
admitted. No new leniency is introduced: a successor checkpoint that exists and is usable is
itself proof that the cycle ran to completion and produced a usable state. A `conflict` produced by
an actual field disagreement SHALL remain a hard gap.

#### Scenario: Absence with proven continuity completes the cycle

- **WHEN** a cycle's cohort terminal rows are succeeded, its successor state entries exist in the index with `usable_flag=True`, and the terminal rows record no init-state identity
- **THEN** the cycle completion verdict is `complete` and the next backfill pass admits the successor cycle as the oldest gap

#### Scenario: Cohort recorded identity participates in the completion verdict

- **WHEN** an accepted-submit cohort candidate reaches terminal success with no completed hydro identity, its journal has a current per-model terminal row carrying the reservation-time init-state identity, and the strict warm-start resolution is ready and names a state
- **THEN** the completion verdict compares the full recorded mapping (state id and every optional identity field present on both sides) against the strict selection
- **AND** a conflict keeps the cycle `gap` even when successor evidence is ready
- **AND** a match makes the cycle `complete` even when successor evaluation returns no evidence
- **AND** if the repository has no full-identity accessor or the current journal rows carry no identity, the verdict remains `absent` and uses the unchanged successor-readiness fallback

#### Scenario: Conflict still gaps

- **WHEN** the terminal row records an init-state identity that conflicts with the strict warm-start resolution
- **THEN** the verdict remains `gap` and no strictness is relaxed

#### Scenario: Missing successor states still gap

- **WHEN** the terminal decision is success and the init-state record is absent but the successor state entries are missing or not usable
- **THEN** the verdict remains `gap`

#### Scenario: Terminal success is evaluated before warm-start admission

- **WHEN** a cycle's candidate has a terminal-success decision in the journal while its strict warm-start resolution is not ready
- **THEN** the verdict path evaluates the terminal decision instead of returning `gap` at the strict early-return, and the candidate is not re-blocked for a run that already finished

#### Scenario: Strict-not-ready with proven continuity completes the cycle

- **WHEN** a cycle's candidate is terminal-success, its strict warm-start resolution is not ready for an allowlisted reason, its successor state entry exists in the index with `usable_flag=True`, and its repository exposes the optional completed-pipeline full-identity accessor
- **THEN** the comparison returns `unverifiable`, the cycle completion verdict is `complete`, and the next backfill pass admits the successor cycle as the oldest gap
- **AND** the full-identity accessor is not invoked because the strict resolution names no state to compare

#### Scenario: Strict-not-ready without proven continuity still gaps

- **WHEN** a cycle's candidate is terminal-success and its strict warm-start resolution is not ready, but no usable successor state entry exists
- **THEN** the verdict remains `gap` — the relaxation is conditioned on physical continuity, never on terminal success alone

#### Scenario: Non-terminal candidates keep today's gating

- **WHEN** a cycle's candidate has no terminal-success decision
- **THEN** the verdict path applies the strict and successor readiness gates exactly as before this change

#### Scenario: Successor evidence "no verdict" is not proof

- **WHEN** the successor-state evaluation returns no evidence at all (the third state: not evaluated, e.g. outside the strict window)
- **THEN** the absence-tolerant branch does not engage and the verdict remains `gap`

#### Scenario: The downstream journal predecessor identity gate is unchanged

- **WHEN** a cycle's verdict becomes `complete` under absence tolerance and discovery proceeds to the journal predecessor identity check
- **THEN** that gate's behavior is byte-identical to today and successor admission still requires it to pass

#### Scenario: Legacy recorded rows are unaffected

- **WHEN** a cycle's terminal rows are legacy per-basin rows carrying `init_state_id` that matches the strict resolution
- **THEN** the verdict is `complete` exactly as before this change

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
record, and no state index mutation. When multiple output roots exist, the VERIFIED root is the
first root that passes the full check AND yields a publishable artifact set (non-empty verified
checkpoints, or the gated final-IC fallback with a verified `final_ic` entry); a root failing
either half falls through to the next — including a witnessed root whose manifest records
requested checkpoint hours but zero captured checkpoints
(`STATE_SAVE_SOURCE_CHECKPOINTS_UNCAPTURED`) and a fallback-lane root whose manifest names no
final IC (`STATE_SAVE_SOURCE_FINAL_IC_MISSING`) — except a present-but-unparseable manifest or
an unsafe declared path, which keep the existing `Invalid state checkpoint manifest` /
`State checkpoint path is unsafe` / `State checkpoint path escapes output directory` hard
errors unchanged, and the legacy oversized-artifact and manifest-entry-count-overflow hard
errors, which likewise never fall through and keep their messages verbatim; a hard error
raised by ANY probed root — first or later — terminates the probe immediately and is the
reported message, superseding an earlier root's fall-through rejection. When no root publishes
and no probed root raised a hard error, the reported reason SHALL be the first existing root's
rejection, byte-identical in the single-root case to the pre-change message. The final-IC fallback
SHALL be reachable only when a verified manifest declares zero checkpoints AND
`provenance.requested_checkpoint_hours` is empty AND no earlier existing root fell through
with `STATE_SAVE_SOURCE_CHECKPOINTS_UNCAPTURED` (the cross-root extension of the no-downgrade
invariant: a root whose rejection PROVED the run requested checkpoint states blocks fallback
publication by every later root; an earlier root that fails before its requested hours are
read — missing manifest, incomplete declarations, checksum drift — does not arm the guard,
matching pre-change fall-through behavior), and SHALL publish only the file the
manifest's `final_ic` entry names, checksum-verified — an undeclared file is never selected,
and a fallback-lane manifest without a `final_ic` entry rejects with
`STATE_SAVE_SOURCE_FINAL_IC_MISSING` when no later root publishes. A
verified manifest with requested hours but zero captured checkpoints rejects with
`STATE_SAVE_SOURCE_CHECKPOINTS_UNCAPTURED` when no later root publishes. The admission contract
is identity plus
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
  `relative_path`/`valid_time`, carrying an unparseable `valid_time`, or referencing a
  non-regular file), or the `checkpoints` value itself is not a list, or a referenced file's
  content no longer matches its declared `checksum`
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
  `STATE_SAVE_SOURCE_FINAL_IC_MISSING` when no later root publishes, instead of searching the
  tree.

#### Scenario: A total checkpoint miss cannot downgrade to the fallback
- **WHEN** a verified manifest records non-empty `provenance.requested_checkpoint_hours` but an
  empty `checkpoints` list (the `STATE_CHECKPOINTS_MISSING` tree retried into state save)
- **THEN** that root never publishes its final IC in place of the requested checkpoint states —
  it yields only to a later CHECKPOINT-publishing root; a later fallback-lane root is likewise
  ineligible (cross-root no-downgrade), and when no eligible root exists the publish is
  rejected with `STATE_SAVE_SOURCE_CHECKPOINTS_UNCAPTURED`.

#### Scenario: A total-miss workspace tree does not shadow a healthy sibling root
- **WHEN** the workspace root holds attempt N+1's witnessed total-miss tree (requested hours
  non-empty, zero captured checkpoints — or, on the fallback lane, a zero-hours manifest naming
  no final IC) and the `output_uri` root holds attempt N's healthy verified tree
- **THEN** the publish succeeds from the object-store root's manifest-named artifacts — the
  unpublishable root yields instead of hard-rejecting — except that a root which fell through
  with `STATE_SAVE_SOURCE_CHECKPOINTS_UNCAPTURED` blocks any LATER root from publishing via the
  final-IC fallback lane (checkpoint-lane siblings remain eligible)
- **AND** when BOTH roots are unpublishable and neither raised a hard error, the reported
  reason is the first existing root's rejection, byte-identical to the single-root message (in
  the reversed geometry — first root failing an always-fall-through reason, later root failing
  on publishable-set — this reports the first root's reason where the pre-change gate surfaced
  the later root's hard token)
- **AND** the hard exceptions (unparseable manifest, unsafe declared path, oversized artifact,
  entry-count overflow) still terminate immediately with their messages verbatim, never
  yielding to a sibling root — including when raised by a LATER root after an earlier root
  fell through: the later root's hard message is reported, superseding the earlier root's
  fall-through reason (when the earlier fall-through is one of the two re-scoped
  publishable-set verdicts these messages change — the pre-change gate hard-rejected at the
  first root without ever opening the sibling; after a pre-existing fall-through reason the
  pre-change gate also probed the sibling and reported the same later-root hard message —
  unchanged; fail-closed and the nonzero exit are preserved either way).

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

### Requirement: Run output tree is attempt-fresh at solve start

Every `SHUDRuntime.execute()` attempt SHALL begin with an output tree
containing no bytes from any prior attempt of the same run: a
pre-existing NON-EMPTY `runs/<run_id>/output` is quarantined aside
(renamed, never followed, never partially cleared) to
`runs/<run_id>/output_residue/previous` before the solve starts,
retaining exactly one residue tree (the most recent non-empty one);
a pre-existing EMPTY `output` directory is reused as-is, with no
quarantine and no eviction of retained residue. The quarantine
sibling SHALL be invisible to the publish admission gate and to
result upload. Hygiene failures split by FAILURE SHAPE: a
tamper-shaped failure — any filesystem-safety refusal, including
the `output` path or quarantine sibling not being a plain
directory, an unsafe or symlinked path component, a containment
violation, or a refusal whose safety is indeterminate — SHALL
terminate the attempt with the permanent typed workspace error
before any solve spawn; an I/O-shaped failure anywhere in the
hygiene steps (the probe and emptiness listing included, not only
clear/rename/recreate) SHALL terminate the attempt with a TRANSIENT
typed storage error (automatic retry preserved), in both cases with
full failure accounting; no hygiene failure may escape as an
untyped generic error. In consequence, the witness manifest's
`final_ic` entry names a file written by the attempt that produced
the manifest.

#### Scenario: Stale final-IC residue is never blessed by a later attempt

- **WHEN** a killed attempt left `<project>.cfg.ic.update` (and any
  other residue) in the run's output tree and a later attempt of the
  same `run_id` runs with zero requested checkpoint hours and a solve
  that writes no final IC
- **THEN** the later attempt's witness manifest contains no
  `final_ic` entry naming the stale file
- **AND** the stale tree is locatable, complete, at the quarantine
  sibling rather than deleted silently.

#### Scenario: Quarantine never follows links and fails closed

- **WHEN** the pre-existing `output` path is itself a symlink, or the
  quarantine rename/clear fails
- **THEN** the attempt terminates before the solve spawns with a
  typed error whose retry classification matches the failure shape —
  permanent for tamper-shaped failures (any filesystem-safety
  refusal: non-directory `output`, a tampered quarantine sibling,
  an unsafe path component), transient for I/O-shaped failures
  anywhere in the hygiene steps — the link
  target is untouched, no partially-cleared tree remains, and the
  failure log, task-outcome receipt, and failed-run transition are
  all recorded.

#### Scenario: Hygiene is invisible outside the attempt

- **WHEN** an attempt quarantines residue and completes successfully
- **THEN** result upload covers exactly the fresh `output` tree, the
  quarantine sibling is never a candidate root for the publish
  admission gate (whose root set — workspace `runs/<run_id>/output`
  plus the `output_uri` root — is unchanged), and lanes that restart
  downstream stages without re-entering `execute()` (durable output
  reuse) observe no change.

### Requirement: Declared state checkpoint hours SHALL be structurally reachable under the configured restart cadence

Forecast manifest assembly SHALL reject, with a stable typed error code, any
combination where a declared state checkpoint hour is not an integer multiple
of `update_ic_step_minutes` — at every manifest production site, not just one
— because SHUD writes a restart file only when the elapsed model time divides
the configured `Update_IC_STEP`, including at END, so a checkpoint hour that
does not divide the cadence can never be produced. The checkpoint recovery
rerun SHALL set the cadence it needs for the hour it is recovering, so
recovery stays correct independently of that
configuration. The cadence written for a recovery rerun applies to that
scratch rerun only; the main run's published configuration is restored
afterwards.

#### Scenario: a misaligned cycle configuration is refused instead of silently unreachable

WHEN allowed cycle hours produce checkpoint hours that are not integer
multiples of the derived restart cadence
THEN forecast manifest assembly fails with a stable typed error code at each
manifest production site, instead of emitting a manifest whose checkpoint
hours can never be written

#### Scenario: recovery rerun makes its target hour reachable

WHEN the checkpoint recovery rerun shortens the run to a missing forecast
hour
THEN the configuration it writes sets the restart cadence to that hour's
minute count, and after the rerun the main run's published configuration text
is byte-for-byte what it was before

#### Scenario: aligned configurations are unaffected

WHEN the configured cycle hours are evenly spaced, including the default
configuration
THEN manifest assembly and checkpoint behavior are unchanged; analysis
manifests, which declare no state checkpoint hours, are outside this
requirement

### Requirement: Cycle completion scope SHALL exclude a model for cycles that predate its own state lineage cutover

The cycle completion verdict SHALL exclude from its scope any model that
acquired its state by a recorded clone from a predecessor model
(`cloned_from_model_id` present on a clone row for that `(model_id, source_id)`
pair), for every cycle whose cycle time is strictly earlier than the clone
row's `valid_time` (`t*`). Such a model SHALL likewise not be admitted into the
execution cohort for those cycles. For cycles at or after `t*` the model SHALL be scored and admitted
exactly as any other model.

`t*` SHALL be resolved per `(model_id, source_id)` — clone rows are written per
source and different sources MAY cut over at different instants. `t*` SHALL be
the model's **own** cutover: the earliest clone row by `(valid_time,
created_at)` written under that same `model_id`, which is the instant the
identity came into existence for that source. Both `clone_gate_kind` values —
`'hydrologic_core'` and `'state_compatibility'` — SHALL confer lineage, and a
clone row whose `clone_gate_kind` is absent or unrecognised SHALL still confer
lineage when `cloned_from_model_id` is present.

A clone row whose `cloned_from_model_id` equals its own `model_id` SHALL NOT
confer lineage and SHALL be disqualified during resolution, on every
persistence plane. Such a row names no predecessor, so it evidences no
existence-start. The disqualification is **per row, not per model**: the
remaining clone rows for that `(model_id, source_id)` SHALL still be considered,
so where a self-referential row precedes a legitimate clone row, `t*` SHALL
resolve to the legitimate row rather than to the self-referential one. A model
whose only clone row is self-referential therefore resolves to no lineage and
SHALL be scored and admitted as a model with no clone row — which keeps it in
scope, the direction that can only leave a loud stuck gap and can never
silently hide completed work.

Lineage resolution SHALL NOT walk the ancestry chain. For a basin recalibrated
more than once (`M → M'` at `t1`, `M' → M''` at `t2`), the boundary for `M''`
SHALL be `t2` and never `t1`: the cycles in `[t1, t2)` were run by `M'`, `M''`
has no history there, and admitting `M''` into their scope would reproduce the
unclosable gap this requirement exists to prevent.

A model with no clone row for the pair SHALL be scored and admitted exactly as
today, with no change to its history-existence signal or to its first-cycle /
cold-start admission path.

This requirement governs completion scope and cohort membership only. The
generation quarantine on the admission side, the strict warm-start lineage
checks, and the derivation of the content-addressed `model_id` are unchanged.

#### Scenario: A recalibrated model does not gap the cycles that predate it

- **WHEN** a model `M'` carries a clone row for `(M', source)` with
  `cloned_from_model_id = M` and `valid_time = t*`, and the completion verdict
  is computed for a cycle whose cycle time is earlier than `t*`
- **THEN** `M'` is absent from the completion scope for that cycle
- **THEN** the cycle's verdict is decided by the remaining in-scope models
  alone, and is `complete` when they all completed it
- **THEN** `M'` is not admitted into that cycle's execution cohort.

#### Scenario: The cutover cycle itself is scored normally

- **WHEN** the completion verdict is computed for the cycle whose cycle time
  equals `t*`
- **THEN** `M'` is in completion scope and the cycle is `complete` only if `M'`
  genuinely completed its pipeline for it
- **THEN** `M'` is admitted into that cycle's cohort and warm-starts from the
  clone row at `valid_time == t*`.

#### Scenario: A model without lineage is unaffected

- **WHEN** the completion verdict is computed for a model that has no clone row
  for the `(model_id, source_id)` pair
- **THEN** the model is in completion scope for every cycle in the window,
  exactly as before this change
- **THEN** its history-existence signal and its first-cycle admission decision
  are byte-for-byte those it would have received before this change.

#### Scenario: A self-referential clone row confers no lineage

- **WHEN** a clone row for `(model_id, source_id)` carries a
  `cloned_from_model_id` equal to that row's own `model_id`
- **THEN** that row is disqualified during lineage resolution on both the
  database and the file state-index plane
- **THEN** if it is the model's only clone row, the model resolves to no
  lineage and stays in completion scope and cohort admission for every cycle
- **THEN** if a legitimate clone row also exists, `t*` resolves to the
  legitimate row's `valid_time`, even when the self-referential row is earlier.

#### Scenario: A twice-recalibrated model is scoped by its own cutover

- **WHEN** a basin was recalibrated twice, yielding clone rows `M → M'` at `t1`
  and `M' → M''` at `t2` with `t1 < t2`, only `M''` is in the active model set,
  and the verdict is computed for `M''`
- **THEN** `M''` is scoped out of every cycle earlier than `t2`, including the
  cycles in `[t1, t2)` that `M'` ran
- **THEN** the resolution consults only clone rows written under `M''`'s own
  `model_id` and performs no ancestry walk.

#### Scenario: A backdated re-activation does not retroactively scope out run cycles

- **WHEN** more than one clone row exists under a single `model_id` for a
  source, the earliest at `t_a` and a later one at `t_b > t_a`
- **THEN** the boundary is `t_a`
- **THEN** cycles in `[t_a, t_b)` that the identity actually ran stay in scope
  and are still required to have genuinely completed.

### Requirement: An empty completion scope SHALL distinguish lineage exclusion from missing configuration

The completion verdict SHALL distinguish a scope that is empty because no
models were configured or all were excluded by source scope — which SHALL keep
its existing `gap` verdict as a misconfiguration guard — from a scope that
became empty because every model was excluded by the lineage cutover filter,
which SHALL NOT be a gap.

#### Scenario: Every model lineage-scoped out is not a gap

- **WHEN** every model in a cycle's source scope carries a lineage cutover
  later than that cycle's cycle time
- **THEN** the cycle is not scored as a gap and is not selected for backfill
  execution.

#### Scenario: No configured models remains a gap

- **WHEN** the completion verdict is computed with no models in scope before
  the lineage filter is applied
- **THEN** the verdict is `gap`, unchanged.

### Requirement: Lineage exclusion SHALL be recorded in evidence as an annotation

Each `(model, cycle)` pair excluded by the lineage cutover filter SHALL be
recorded in the pass evidence with a distinct reason naming the exclusion, the
predecessor `model_id`, and the resolved `t*`. The record SHALL be an
annotation: it SHALL NOT be read back as an input to any completion,
admission, or selection decision.

#### Scenario: An operator can tell scoping from genuine completion

- **WHEN** an operator reads the pass evidence for a cycle that scored
  `complete` while a recalibrated model was scoped out of it
- **THEN** the evidence names that model, its predecessor `model_id`, and the
  resolved `t*` under a distinct lineage-exclusion reason
- **THEN** the operator can distinguish this cycle from one where every model
  genuinely completed, without re-deriving the lineage.

### Requirement: The stale-identity breaker SHALL NOT re-engage on lineage-excluded cycles

A cycle SHALL keep its `complete` verdict when that verdict follows from a
recalibrated model being lineage-excluded: the journal predecessor-identity
staleness breaker, which evaluates cycles that otherwise score `complete`,
SHALL NOT re-flip it to `gap` on account of the predecessor model's journal
rows still carrying the predecessor's identity tokens.

#### Scenario: Predecessor journal rows do not re-engage the breaker

- **WHEN** a cycle earlier than `t*` scores `complete` with `M'` lineage-
  excluded, and that cycle's journal still holds `M`'s rows with `M`'s identity
  tokens
- **THEN** the breaker does not engage for that cycle and the verdict stays
  `complete`.

### Requirement: Backfill predecessor emission SHALL NOT prepend a candidate that predates its model's lineage cutover

The backfill lane SHALL NOT emit a predecessor candidate for a model at a
cycle earlier than that model's resolved `t*`, when it prepends a predecessor
ahead of a selected cycle.

#### Scenario: No unrunnable prepend at the cutover boundary

- **WHEN** the cycle at `t*` is selected and the predecessor-emission path is
  consulted for `M'`
- **THEN** no predecessor candidate is emitted for `M'` at the cycle before
  `t*`
- **THEN** `M'`'s warm start for the cycle at `t*` resolves to the clone row at
  `valid_time == t*`.

### Requirement: History-existence SHALL be scoped to state entries at or before the candidate's own cycle time

The §8 history-existence signals SHALL count only usable state-index entries whose `valid_time`
is at or before the candidate's cutoff — the candidate's own cycle time. This governs
`history_exists_any_generation` and its current-generation counterpart alike. An entry at `valid_time == cutoff` (the
exact-predecessor location) SHALL continue to count as history, because it proves the model was
previously exercised. An entry at `valid_time > cutoff` SHALL NOT count: such an entry can only
have been produced by the candidate's own run or by a later cycle, and a run's own output SHALL
NOT be treated as evidence that the run had a predecessor.

This closes the general form of the defect that the lineage-cutover scope-outs address per-case
for recalibrated models: a state entry whose `valid_time` postdates the candidate makes
history-existence true at every cycle, which permanently closes the packaged-IC bootstrap branch
for a model's own first cycle and drives the backward-recursion dead chain in §8.6 predecessor
emission. Models carrying a state-lineage cutover keep their existing scope-out behavior
unchanged; this requirement is what makes newly onboarded models — which carry no cutover —
behave correctly without one.

The exact-predecessor lookup key SHALL be unaffected: it resolves at `valid_time == cutoff`,
which remains inside the scope.

#### Scenario: A model's own first-cycle output does not close its bootstrap branch

- **WHEN** history-existence is computed for a newly onboarded model's first cycle, and the only usable state-index entries for that model and source carry `valid_time` later than the cycle time — the entries that cycle's own run produced
- **THEN** history-existence is false, the packaged-IC bootstrap branch stays open for that cycle, and no exact predecessor is demanded from a cycle that never existed

#### Scenario: An entry at the exact predecessor location still counts as history

- **WHEN** a usable state-index entry exists at `valid_time == cutoff`
- **THEN** history-existence is true, unchanged from before this requirement

#### Scenario: Established models are unaffected

- **WHEN** history-existence is computed for a model whose usable state-index entries begin well before the candidate's cycle time
- **THEN** the signal is true exactly as before, and the candidate's branch selection is unchanged

#### Scenario: Scoping does not disturb the exact-predecessor lookup

- **WHEN** an exact-predecessor entry is present at the expected identity key
- **THEN** it is still found and classified (including the wrong-generation quarantine path) exactly as before, because the expected key's `valid_time` equals the cutoff and remains in scope

#### Scenario: A not-ready reason outside the allowlist keeps the hard gap

- **WHEN** a cycle's candidate is terminal-success, its successor state entry is usable, and its strict warm-start resolution is not ready for a reason reporting a defect in an existing state — a lineage or checksum mismatch, an unusable or unreadable checkpoint, or a prior checkpoint missing after history
- **THEN** the comparison returns `conflict`, the verdict remains `gap`, and the successor-continuity tolerance is not applied

#### Scenario: The allowlist is closed against new reasons

- **WHEN** the strict warm-start resolution is not ready for a reason that is not named in the allowlist, whatever that reason is
- **THEN** the comparison returns `conflict` — admission to the relaxation is by explicit enumeration, never by failing to exclude

