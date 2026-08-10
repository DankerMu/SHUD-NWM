# cross-cycle-warm-start-chaining (delta)

## MODIFIED Requirements

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
errors, which likewise never fall through and keep their messages verbatim. When no root
publishes, the reported reason SHALL be the first existing root's rejection, byte-identical in
the single-root case to the pre-change message. The final-IC fallback
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
- **AND** when BOTH roots are unpublishable, the reported reason is the first existing root's
  rejection, byte-identical to the single-root message (in the reversed geometry — first root
  failing an always-fall-through reason, later root failing on publishable-set — this reports
  the first root's reason where the pre-change gate surfaced the later root's hard token)
- **AND** the hard exceptions (unparseable manifest, unsafe declared path, oversized artifact,
  entry-count overflow) still terminate immediately with their messages verbatim, never
  yielding to a sibling root.

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
