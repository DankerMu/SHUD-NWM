# cross-cycle-warm-start-chaining (delta)

## ADDED Requirements

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
