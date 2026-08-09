# Design: state-save-source-freshness-gate

Line anchors verified on `f2a9925e` (Phase 0 explorer map + fixture
run-1 and run-2 reviewer walks). Labels: "run-1 rN" = the
terminated first run's fixture rounds (history in tasks.md §0/1.2);
"run-2 rN" = the current run's rounds; bare "r2 P1" etc. in older
paragraphs refer to run-1 rounds. Run-1 round-1 repairs: wall-clock
anchor
replaced by solver-success witness (P1-1/P1-2); spec MODIFIED block
carries all six base scenarios (P1-3); failure lanes stay
manifest-less (P1-4); `tests/test_production_scheduler.py` added to
regressions (P2-1); multi-root ruling pinned (P2-2); `run_attempt`
dropped for `slurm_job_id`+`array_task_id` (P2-3). Run-1 round-2
repairs:
the fallback lane is now manifest-driven and checksummed — writer
records `requested_checkpoint_hours` + a `final_ic` entry, the rglob
is retired, total-miss trees reject (r2 P1); checksum-absent entries
reject (r2 P2-1); A10 restated with the manifest-less happy-path
sites enumerated (r2 P2-2); A6(a) relabeled with a genuinely red
foreign-provenance variant added (r2 P2-3); symlink/escape oracle
pins added to A8 (r2 P2-4).

## D1. Admission gate — predicate order and typed reasons

Gate runs at the top of `save_state_for_run`
(`packages/common/state_cli.py:128`), after context resolution,
BEFORE any artifact selection (`_find_state_checkpoints`; the
`_find_ic_file` rglob is deleted by this change). Roots are handled
per D3; within a candidate root, predicates in order — first
violation produces that root's typed reason (`StateManagerError`
message STARTS with the token, machine-greppable):

| # | Predicate | Typed reason | Master behavior displaced |
|---|---|---|---|
| G1 | ≥1 output root resolves AND exists (workspace `runs/<run_id>/output`, `output_uri` dir — the two DIRECTORY root families probed today, `:560-617`; `_find_ic_file`'s third geometry — `output_uri` resolving to a single FILE object via `:571-577`/`:702-706` — is retired with it: no producer emits a file-shaped `output_uri`, disclosed in proposal delta 6) | `STATE_SAVE_SOURCE_OUTPUT_MISSING` | silent `[]` → rglob fallback → best case untyped "No .cfg.ic found" |
| G2 | `<root>/state_checkpoints/state_checkpoints.json` present (full path level per `state_cli.py:600-611` / `runtime.py:3092-3095`) | `STATE_SAVE_SOURCE_MANIFEST_MISSING` | silent fallback to rglob |
| G3 | manifest carries `provenance` block with required `run_id` + `generated_at` + `requested_checkpoint_hours` (the post-gate branch keys on it — a provenance block missing it is a G3 violation, fail-closed, r3 note); `slurm_job_id`/`array_task_id` keys present (values may be null/int-null outside Slurm) | `STATE_SAVE_SOURCE_PROVENANCE_MISSING` | n/a (new schema; legacy manifests land here — D5 ruling) |
| G4 | `provenance.run_id == context.run_id` | `STATE_SAVE_SOURCE_PROVENANCE_MISMATCH` | n/a |
| G5 | every manifest-declared artifact (checkpoint entries; on the fallback lane also the `final_ic` entry) present (stat succeeds), CARRYING a declared `checksum` (absence is itself a violation — r2 P2-1; both writers always emit it, `runtime.py:3197`, `:3162`), AND content hash equal to it — hashing reads through the SAME `MAX_STATE_IC_BYTES` bounded-read the normalization path uses, and an oversized artifact re-raises the existing `state checkpoint IC file exceeds size limit` message unchanged (r3 P2 read-bound ruling, A8 pin); evaluated for ALL entries, offending sets named in the message | missing file or missing checksum → `STATE_SAVE_SOURCE_MANIFEST_INCOMPLETE`; hash mismatch → `STATE_SAVE_SOURCE_ARTIFACT_CHECKSUM_MISMATCH` | `:652-655` silent `continue`, publishes M<N; checksums never verified |

No wall-clock predicate (round-1 P1-1/P1-2). The witness statement,
stated precisely (r2 P1 precision defect): manifest presence proves
that SOME attempt of this run_id completed its solve successfully in
this tree; what makes any particular published byte trustworthy is
that the manifest NAMES and CHECKSUMS it. The gate therefore never
publishes an undeclared file — declared checkpoints are
checksum-pinned, and the fallback lane publishes only the
manifest-named `final_ic` (below). A failed attempt writes no
manifest, so its residue rejects at G2; a previous SUCCESSFUL solve
of the same run_id is a legitimate artifact of the same run identity
and is deliberately admissible — this keeps the
`durable_shud_output_reused=True` retry lane alive; and a later
KILLED attempt that rewrites files in place is caught by the
checksum half of G5 (declared entries are copies under
`state_checkpoints/` made atomically at capture, `runtime.py:
2010-2016`, `:2134-2135`; the fallback's `final_ic` lives in the
rewritten root and is exactly why it needs its own checksum).

After G1-G5 pass on a root, branch on the verified manifest (r2 P1):
- `checkpoints` non-empty ⇒ existing per-entry processing
  (`_checkpoint_with_header_time`) applied to the VERIFIED root's
  manifest entries directly — no re-probe via
  `_find_state_checkpoints` (run-2 r2 P3-4: re-probing would pick
  the first non-empty manifest by probe order and defeat the A6(b)
  foreign-provenance fall-through).
- `checkpoints` empty ∧ `provenance.requested_checkpoint_hours`
  empty ⇒ final-IC fallback: publish EXACTLY the file the manifest's
  `final_ic` entry names, checksum-verified (G5 applies to it),
  `valid_time=run.end_time` stamping retained, and the fallback's
  `original_shud_filename` comes from the entry (keeping the
  existing `snapshot.original_shud_filename` oracles intact,
  `tests/test_warm_start_chaining.py:227`,
  `tests/test_state_manager.py:1774`) — this is the analysis lane's
  and short-horizon forecasts' normal publish path, now fully
  witnessed. `final_ic` entry ABSENT on this lane ⇒ reject
  `STATE_SAVE_SOURCE_FINAL_IC_MISSING` (its own token — G5 is
  defined over declared artifacts, an absent entry needs a named
  branch; run-1 r3 P1); entry present but file missing/checksum-less
  /mismatched ⇒ G5 reject. The `_find_ic_file` rglob is RETIRED from
  the publish path (deleted; sole caller is `save_state_for_run`) —
  an undeclared file in the tree is never selected, so a later
  killed attempt's mid-horizon `*.cfg.ic.update` cannot be
  published, and the publish-side header rewrite
  (`state_cli.py:240-248`) can no longer forge agreement for a
  stale artifact.
- `checkpoints` empty ∧ requested hours NON-empty (the
  `STATE_CHECKPOINTS_MISSING` total-miss tree retried into state
  save) ⇒ reject `STATE_SAVE_SOURCE_CHECKPOINTS_UNCAPTURED` — the
  fallback is not a downgrade lane for trees that failed their own
  capture contract (r2 P1 second sub-case).

A manifest that exists but fails to PARSE (oversized, symlink,
malformed) keeps the existing `Invalid state checkpoint manifest`
error — that check sits between G2 and G3 and is a hard reject for
the run (no root fall-through; a corrupted manifest is suspect, and
the existing assertions in `tests/test_state_manager.py:2264-2292`
keep their message unchanged — A8 pin). Likewise a declared entry
whose path is a symlink or escapes the output root keeps the
existing `State checkpoint path is unsafe` / `State checkpoint path
escapes output directory` errors (`state_cli.py:646-658`) — G5 must
re-raise them unchanged, never fold them into `MANIFEST_INCOMPLETE`
(r2 P2-4; hard reject, no fall-through). Within G5, the unsafe-path
/escape checks run BEFORE the checksum-presence check for each
entry (run-2 r1 P3-3 — the A8 symlink pin's fixture entry carries
no checksum, and the unsafe message must still win). G5's
declared-entry set is the RAW `checkpoints` array (run-2 r1 P2):
any entry the current loader silently drops — non-dict (`:643`),
missing `relative_path`/`valid_time` (`:646-647`), missing file
(`:654-655`), non-regular target incl. directories (`:658-659`) —
is itself a `STATE_SAVE_SOURCE_MANIFEST_INCOMPLETE` violation
naming the entry index; and a `checkpoints` value that is not a
sequence (e.g. `{}`, today parsed as empty at `:631-633`) is
likewise `MANIFEST_INCOMPLETE`, never the fallback lane. The G5
hash/integrity pass is performed BY THE GATE over the raw array —
`_load_state_checkpoint_manifest`'s parse semantics are unchanged
(run-2 r2 P3-1: the loader-direct tolerance test
`tests/test_state_manager.py:2155-2217` feeds fake checksums and
must stay green; checksum verification never moves into the
loader).

## D2. Writer changes (`workers/shud_runtime/runtime.py`)

1. `write_manifest` (`:3085-3109`): remove the no-targets early
   return (`:3090-3091 if not self.targets: return`). The CALLER
   geometry is already correct and unchanged: the single call at
   `:598` sits after the spawn-failure/`SHUD_TIMEOUT`/`SHUD_EXIT_n`
   raises (`:558-573`) and before the `STATE_CHECKPOINTS_MISSING`
   post-solve gate (`:601-616`) — so the invariant "manifest exists
   ⟹ this run_id's solver completed successfully" HOLDS today and
   is preserved, now extended to zero-checkpoint-hour runs (empty
   `checkpoints` list instead of no file). Failure lanes keep NOT
   writing; nothing new is written on failure (round-1 P1-4: a
   failure-lane manifest would hand KILLED/partial trees a
   provenance pass through the gate — explicitly rejected design).
   SUPERSESSION (run-2 r1 P1): this widening deliberately inverts
   the #1315 r3-audit guard
   `test_run_shud_writes_no_checkpoint_manifest_when_no_hours_are_
   requested` (`tests/test_shud_runtime.py:4067-4091`, asserts
   `not (output_dir / "state_checkpoints").exists()` on a
   successful zero-hour solve). Its recorded rationale — "no
   consumer starts seeing an empty checkpoint index where there was
   previously no file to read" — is answered: the sole production
   reader is now the gate itself, the MODIFIED tolerance scenario
   covers key tolerance, and an empty `checkpoints` list is no
   longer a downgrade signal because `requested_checkpoint_hours`
   discriminates the lanes. The test is REWRITTEN (not deleted) as
   the inverted anchor A7(a3); this is the one enumerated exception
   to A10's oracle-integrity rule.
2. Payload gains top-level `provenance`:
   `{"run_id": <run id>, "generated_at": <UTC ISO at write time>,
   "slurm_job_id": <str|null>, "array_task_id": <int|null>,
   "requested_checkpoint_hours": <the hour list the run was asked
   to capture, [] when none>}` — job facts from the same env source
   as `_task_outcome_attempt_identity` (`:1981-1994`:
   `SLURM_ARRAY_JOB_ID`/`SLURM_JOB_ID`/`SLURM_ARRAY_TASK_ID`);
   requested hours are the tracker's own `targets`. No `run_attempt`
   field (round-1 P2-3: the runtime has no attempt-ordinal fact; the
   job id is what discriminates executions — recorded for evidence,
   not hard-matched).
3. Payload gains top-level `final_ic` (r2 P1; discovery rule pinned
   per run-1 r3 P1): the final IC is identified by EXACTLY two
   candidate paths — the tracker's own watched
   `output_dir/<project_name>.cfg.ic.update` (`runtime.py:3025`
   `source_path`), and if that does not exist,
   `output_dir/<project_name>.cfg.ic`. No recursive search, no
   other filename is ever recorded; a candidate anywhere else is
   treated as "no final state" (this is what keeps a residue file
   under a different name from being blessed by the writer — the
   r3 P1 relocation hazard). When one exists, record
   `{"relative_path": ..., "original_shud_filename": ...,
   "checksum": <same stdlib SHA-256 as `_capture`,
   `runtime.py:3197`>}` computed at manifest-write time; the read
   must not raise into the `:617` manifest_error re-raise path (a
   read failure records no `final_ic`, it must not turn a
   successful run into a failed one). Absent when neither exact
   path exists. Existing keys (`checkpoints`,
   `observed_header_minutes`, `recovery_outcomes`) unchanged.

## D3. Multi-root ruling (round-1 P2-2)

The two root families can disagree: failure-lane trees exist only in
the workspace root (the success path uploads via `upload_results`,
`runtime.py:408`; the failure lane uploads logs only, `:424` — r2
citation fix), so "workspace has a newer-but-failed tree,
object-store has the successful one" is a normal geometry, not an
edge case. Note the env-context lane hardcodes `output_uri=None`
(`state_cli.py:535`), so `--run-id` db-free saves have exactly one
root; the fall-through narrative below applies to the
`--manifest-index` lane (which carries `output_uri`,
`scheduler_candidate_manifest.py:220`). Ruling:

1. Enumerate candidate roots in the existing probe order (workspace,
   then `output_uri`/object-store — same order as
   `_find_state_checkpoints`, `:598-617`).
2. A root that does not exist is skipped; if NO root exists → G1
   reject.
3. For each existing root, evaluate G2-G5. The FIRST root that
   passes G2-G5 fully is the verified root; later roots are ignored.
4. A root failing G2-G5 falls through to the next existing root —
   EXCEPT the unreadable-manifest case (D1: hard reject, no
   fall-through).
5. If no existing root passes, reject with the FIRST existing
   root's reason (deterministic, matches probe order).

Consequence for the disagreement geometry: a workspace tree from a
failed attempt (no manifest) falls through at G2 and the
object-store success tree verifies and publishes — the gate picks
the witnessed root, not the first-probed root. The fallback (empty
`checkpoints`) binds to the verified root only.

## D4. Evidence and exit-code plane

No pipeline-event write from the CLI (proposal Ruling 6). Contract:
typed token leads the `StateManagerError` message → CLI exit 1 via
the existing catch (`state_cli.py:717-789`) → Slurm task fails →
orchestrator's generic array-stage classification records the failed
`state_save_qc` stage. The token lands in Slurm stderr/logs only —
candidate records see a generic stage failure (tasks Deviation 1;
spec scenario wording matches this strength, no overpromise).
Anchors assert: exception type + leading token, zero
`save_state_snapshot`/`run_qc`/index calls on capture fakes;
CLI-level anchor asserts exit 1 with the token on stderr (reusing
`_state_cli_exit_code`, `tests/test_state_manager.py:3135`).

## D5. Legacy/deployment ruling

Writer + gate ship atomically. Rejected population after merge: run
trees whose forecast executed pre-merge (no provenance / no
manifest) — typed reasons `PROVENANCE_MISSING`/`MANIFEST_MISSING`
make the signature diagnosable from evidence alone; recovery is a
forecast re-run. No grace window (a grace window re-opens the #1164
hole for exactly the trees whose origin cannot be proven). Disclosed
as proposal behavior delta 3. `env`/DB/manifest-entry context
constructors are untouched, so there is no mixed-version context
geometry (round-1 P1-2's `CONTEXT_INCOMPLETE` lane no longer
exists).

## D6. Known limits (recorded, not anchored)

- The witness proves solver completion, not chain success: a tree
  with a PARTIAL captured subset (some requested hours captured,
  some abandoned within budget rules) publishes its declared subset
  if retried into state save — requested-vs-captured completeness
  for the non-empty case and run-status truth stay with the
  orchestrator (proposal Non-goal 4). The TOTAL-miss case no longer
  downgrades (G-branch `CHECKPOINTS_UNCAPTURED`, r2 P1).
- The r1 corrupt-fallback residual is CLOSED by the manifest-driven
  fallback (r2 P1): a later killed attempt's in-place rewrite of the
  final IC fails the `final_ic` checksum; an undeclared mid-horizon
  `*.cfg.ic.update` is never selected. What remains: two SUCCESSFUL
  solves of the same run_id in the same tree (attempt 2's manifest
  atomically replaces attempt 1's) — the gate publishes whichever
  complete witnessed tree is present, which is the identity
  contract, not a defect.
- The base spec's manual short-rerun repair lane ("explicit short
  reruns … as manual repair for already completed historical
  cycles") produces publishable trees ONLY if the repair goes
  through `SHUDRuntime.run_shud` (which writes the witness); a
  hand-assembled tree is unpublishable post-merge by design —
  operators repairing history must use the runtime lane (r2 note,
  recorded).
- `_durable_shud_output_exists` (`scheduler_state_failure.py:63-75`)
  remains bookkeeping-only (issue out-of-scope); the gate is now the
  real backstop its comment assumed.
- Object-store G5 stat/hash follows the existing
  `_load_state_checkpoint_manifest` containment-root mechanics
  (`:620-669`); both probe families keep their existing resolution
  code — the gate adds predicates, not new I/O paths.

## D7. Anchors (RED-proofed on pre-change tree unless marked)

Fixture vocabulary: `tests/test_warm_start_chaining.py` in-memory
repo/manager doubles + real files under
`workspace/runs/<run_id>/output` (explorer §5);
`_state_cli_exit_code` (`tests/test_state_manager.py:3135`);
`tests/test_shud_runtime.py` writer harness (its `_manifest()`
fixtures at `:167-197` lack `state_checkpoint_hours`, so `run_shud`
reaches `:598` with `write_manifest` early-returning — exactly
A7(a)'s red). "Witness-bearing fixture" = manifest with `provenance`
matching the test context's run_id.

- **A1 output root missing** (issue AC-1): context valid, no output
  dir anywhere ⇒ token `STATE_SAVE_SOURCE_OUTPUT_MISSING`; zero
  save/QC/index calls. RED on master (master falls to `_find_ic_file`
  → wrong, untyped error; the typed-token + call-absence assertions
  are the red).
- **A2 manifest incomplete** (AC-2): witness-bearing manifest
  declares 2 checkpoints, 1 file deleted ⇒
  `STATE_SAVE_SOURCE_MANIFEST_INCOMPLETE` naming the missing entry;
  no publish. RED on master (publishes the surviving 1 — the exact
  `:652-655` behavior). Companion A2b: file present, content
  altered ⇒ `..._ARTIFACT_CHECKSUM_MISMATCH`. RED. Companion A2c
  (run-2 r1 P2; PARAMETERIZED over all five malformed shapes —
  run-2 r2 P2-1): 2 declared entries where the offending one is
  (i) not a dict (`:642-643`), (ii) a dict missing
  `relative_path`/`valid_time` (`:646-647`), (iii) a dict missing
  `checksum`, (iv) referencing a non-regular target/directory
  (`:658-659`), or — as a whole-manifest case — (v)
  `checkpoints: {}` non-sequence (`:631-633`) ⇒ each rejects
  `..._MANIFEST_INCOMPLETE` naming the entry index (case v names
  the field); ALL five RED on master (loader silently drops the
  entry and publishes the survivor for i/ii/iv, publishes the
  checksum-less entry unchecked for iii, parses-as-empty and takes
  the fallback for v).
- **A3 failed-attempt residue** (AC-3, re-anchored per round-1
  P1-1): output dir exists with a leftover `*.cfg.ic` but NO
  manifest (the failure-lane invariant) ⇒
  `STATE_SAVE_SOURCE_MANIFEST_MISSING`; no `valid_time=end_time`
  stamping. RED on master (rglob publishes). This is the issue's
  KILLED-run geometry expressed through the witness contract.
- **A4 legacy manifest** (AC-6): manifest without `provenance` ⇒
  `STATE_SAVE_SOURCE_PROVENANCE_MISSING`. RED. Companion A4b:
  `provenance.run_id` foreign ⇒ `..._PROVENANCE_MISMATCH`. RED.
- **A5 fallback lane** (r2 P1 closure set): (a) zero-checkpoint
  liveness pin — witness manifest with `checkpoints: []`,
  `requested_checkpoint_hours: []`, `final_ic` present+matched ⇒
  publish succeeds, `valid_time == run.end_time` (outcome GREEN on
  master too; the pin is that the gate must not kill the analysis /
  short-horizon lane). (b) undeclared-artifact selection — SAME
  geometry plus a second, stale residue file in the SAME candidate
  class as the named final IC (both `.cfg.ic.update` or both
  `.cfg.ic` — run-2 r1 P3-2: master prefers ANY `.cfg.ic.update`
  over any `.cfg.ic`, `:586-589`, so a cross-class pair is not
  red), residue name sorting BEFORE the named file ⇒ gate publishes
  exactly the manifest-named file; RED on master
  (`_find_ic_file`'s `sorted()` first-pick selects the residue).
  (c) killed-later-attempt
  corruption — `final_ic` file content altered after manifest write
  ⇒ `STATE_SAVE_SOURCE_ARTIFACT_CHECKSUM_MISMATCH`; RED on master
  (publishes corrupt bytes). (d) total-miss no-downgrade —
  `requested_checkpoint_hours` non-empty, `checkpoints: []` ⇒
  `STATE_SAVE_SOURCE_CHECKPOINTS_UNCAPTURED`; RED on master
  (fallback publishes the final IC in place of requested
  checkpoints). (e) final_ic-absent reject — witness manifest,
  `requested_checkpoint_hours: []`, `checkpoints: []`, NO
  `final_ic` entry ⇒ `STATE_SAVE_SOURCE_FINAL_IC_MISSING`; RED on
  master (rglob publishes whatever it finds).
- **A6 multi-root** (round-1 P2-2, r2 P2-3 relabel): (a) workspace
  root has failed-attempt residue (no manifest), object-store root
  has the witnessed tree ⇒ publish succeeds from the verified root —
  GREEN both sides in outcome (master's `_find_state_checkpoints`
  also skips a nonexistent workspace manifest, `:598-617`); kept as
  the D3 fall-through liveness pin. (b) foreign-manifest fall-through
  — workspace root holds a NON-empty manifest whose
  `provenance.run_id` is foreign, object-store root holds this run's
  witnessed tree ⇒ gate rejects the workspace root at G4, falls
  through, publishes from the object-store root; RED on master
  (first non-empty manifest wins regardless of origin — master
  publishes another run's checkpoints under this run's identity).
  (c) `durable_shud_output_reused` liveness (round-1 P1-2): intact
  witnessed tree, repeat `save_state_for_run` invocation (retry
  geometry) ⇒ publishes; tree deleted ⇒
  `..._OUTPUT_MISSING`/`..._MANIFEST_MISSING`. Intact half GREEN
  both sides (liveness pin); deleted half RED on master in its
  typed-token assertion.
- **A7 writer** (`tests/test_shud_runtime.py`): (a) zero requested
  hours ⇒ manifest written with empty `checkpoints`, provenance
  (`requested_checkpoint_hours: []`), and a `final_ic` entry naming
  + checksumming **`demo.cfg.ic`** — the mock solver
  (`tests/mock_shud_omp.py:65`) writes `demo.cfg.ic` and never
  `demo.cfg.ic.update`, so this anchor exercises the discovery
  rule's second exact path (RED: today `:3090` early-returns;
  `test_run_shud_allows_valid_python_runtime_script` `:2925-2935`
  reaches `:598`); (a2) discovery-rule negative — a residue IC
  under a DIFFERENT name (or in a subdirectory) in the output root
  is NOT recorded as `final_ic` (writer-side pin of the two-exact-
  paths rule; RED: rule doesn't exist); (a3) the superseded #1315
  zero-hour guard (`tests/test_shud_runtime.py:4067-4091`)
  REWRITTEN inverted: successful zero-hour solve ⇒ manifest
  present, `checkpoints: []`, `requested_checkpoint_hours: []`,
  `final_ic` naming `demo.cfg.ic.update` (that harness's
  `_FAST_SOLVER_STUB` writes it; the existing assertion at `:4090`
  proves it) — supersession rationale recorded in D2.1;
  (b) provenance fields
  present, `generated_at` parseable UTC, job facts from env,
  requested hours recorded when non-empty (RED); (c) failure lane
  (`SHUD_EXIT_n`/timeout) writes NO manifest (GREEN both sides —
  witness-invariant pin); (d) `STATE_CHECKPOINTS_MISSING` lane
  still writes the manifest with trails — now including its
  non-empty `requested_checkpoint_hours` (regression pin of the
  base scenario).
- **A8 oracle-integrity pins** (r2 P2-4 + r3 P2 extended): existing
  oversized/symlink MANIFEST tests keep asserting
  `Invalid state checkpoint manifest`
  (`tests/test_state_manager.py:2264-2292`); the symlink
  checkpoint-ENTRY test (`tests/test_state_manager.py:2219-2261`,
  with its fixture upgraded to witness-bearing) keeps asserting
  `State checkpoint path is unsafe` — G5 re-raises the unsafe-path
  and escape errors (`state_cli.py:646-658`) unchanged, never
  folding them into `MANIFEST_INCOMPLETE`; the bounded-IC-read
  oracle (`tests/test_state_manager.py:2100-2124`, fixture upgraded
  to witness-bearing) keeps asserting
  `state checkpoint IC file exceeds size limit` — G5's hashing uses
  the same `MAX_STATE_IC_BYTES` bounded read (D1 ruling), so the
  message survives; diagnostic-key tolerance test
  (`tests/test_state_manager.py:2155-2217`) stays green with the
  provenance/final_ic keys added.
- **A9 CLI exit-code**: a gated reject exits 1 with the token on
  stderr (`_state_cli_exit_code` vocabulary). RED (token new).
- **A10 regressions** (AC-7 + round-1 P2-1 + r2 P2-2): existing
  happy paths stay green by upgrading fixtures to witness-bearing
  manifests (provenance + `requested_checkpoint_hours` + checksummed
  entries / `final_ic`) — where NO manifest exists today one is
  fabricated wholesale, not merely "provenance added". Enumerated
  manifest-less sites: `tests/test_warm_start_chaining.py:164-168`
  (analysis run, workspace root, only `demo.cfg.ic.update`),
  `:239-242` (object-store root only);
  `tests/test_state_manager.py:1701-1774` (db-free manifest-index
  lane, single root, exit 0 + usable index entry — r3 P2),
  `:1785-1804` (db-free env lane, asserts exit 0 + usable index
  entry), `:2100-2124` (bounded-read oracle, see A8 — r3 P2).
  Provenance-less manifest sites:
  `tests/test_warm_start_chaining.py:316-336`, `:432-520`
  (rekey-on-lagged-header happy path — r3 P2),
  `tests/test_state_manager.py:2234-2250`,
  `tests/test_production_scheduler.py:31685-31727`. No existing
  ASSERTION is weakened (oracle-integrity rule), with ONE enumerated
  exception: the superseded #1315 zero-hour guard
  (`tests/test_shud_runtime.py:4067-4091`) is rewritten inverted as
  A7(a3) with its supersession rationale recorded in D2.1 (run-2 r1
  P1). All other fixture edits only gain witness facts.
