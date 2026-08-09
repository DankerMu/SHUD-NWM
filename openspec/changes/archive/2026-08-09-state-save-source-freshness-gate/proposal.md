# Proposal: state-save-source-freshness-gate

## Why

Issue #1325 (priority:high; #1164 field incident's other half, routed
from #1202's out-of-scope list): `save_state_for_run`
(`packages/common/state_cli.py:128-210`) publishes a successor basin
state with NO admission gate on whether the source run actually
produced the output it is about to publish. Today: (1) nothing checks
output-root existence or artifact origin — the only failure exit is
"no candidate file found at all"; (2) checkpoint-manifest entries
whose files are missing are silently skipped
(`_load_state_checkpoint_manifest`, `state_cli.py:652-655`
`except FileNotFoundError: continue`) — declared N, published M<N;
(3) when no manifest is found, `_find_ic_file`
(`state_cli.py:560-590`) rglobs ANY `*.cfg.ic`/`*.cfg.ic.update`
under the run dir and the caller stamps it
`valid_time=run.end_time` (`:154-162`) — an IC left by a KILLED or
partial attempt is published as this cycle's end state; (4) the
scheduler's strict-warm-start successor retry
(`services/orchestrator/scheduler_candidates.py:2227-2230`) restarts
candidates at `state_save_qc` with `durable_shud_output_reused=True`
on the recorded assumption that "downstream artifact guards reject
the retry if that durable output is no longer available" — a guard
that does not exist. Result: a KILLED or retention-cleaned run
publishes a successor state indistinguishable from a healthy one
downstream (`run_qc` only validates the uploaded object,
`state_manager.py:435-467`), and the error propagates along the
warm-start chain unrecoverably. This happened live in the #1164
six-basin replay.

## Ruling

Publish-side admission is **fail-closed, anchored on a
solver-success witness plus run identity plus artifact integrity —
deliberately NOT on wall-clock recency** (fixture round-1 P1-1/P1-2:
`hydro_run.start_time` is simulation time ≡ `cycle_time`, written
once and never updated per attempt — a wall-clock predicate built on
it is vacuously true on the DB path; and on the
`durable_shud_output_reused` retry path there is no new forecast
execution, so any "newer than this submission" clock predicate would
permanently reject the exact recovery lane the scheduler designed.
Identity + success-witness + integrity is the sound contract):

1. **Solver-success witness (writer invariant, kept and widened)**:
   today `state_checkpoints.json` is written only after the solve
   completed (`workers/shud_runtime/runtime.py:598`, after the
   spawn-failure/`SHUD_TIMEOUT`/`SHUD_EXIT_n` raises at
   `:558-573`), but `write_manifest` returns early when no
   checkpoint hours were requested (`:3090-3091`). The change: the
   early return is removed so EVERY successful solve writes the
   manifest (zero-checkpoint-hour configs — the #1317 family — get
   an empty `checkpoints` list), and the manifest gains a top-level
   `provenance` block (`run_id`, `generated_at` UTC at write,
   `slurm_job_id`, `array_task_id`, `requested_checkpoint_hours` —
   job facts from the same env source as
   `_task_outcome_attempt_identity`, `:1981-1994`) plus a top-level
   `final_ic` entry (relative path + original filename + checksum of
   the solve's final state, when one exists). The failure lanes
   (spawn failure, timeout, nonzero exit) continue NOT to write —
   **manifest presence is a witness that an attempt of this run_id
   completed its solve successfully in this tree, and every artifact
   the manifest names is checksum-pinned**. The
   `STATE_CHECKPOINTS_MISSING` lane (post-solve gate failure) keeps
   writing, as today.
2. **Admission gate** in `save_state_for_run`, BEFORE any artifact
   selection (design D1, predicates G1-G5): output root exists; a
   provenance-bearing manifest is present
   (`<root>/state_checkpoints/state_checkpoints.json`);
   `provenance.run_id` equals the context run id; every
   manifest-declared artifact is present, carries a declared
   checksum (absence is itself a violation), and matches it.
   Violations reject with typed reasons
   (`STATE_SAVE_SOURCE_OUTPUT_MISSING` / `..._MANIFEST_MISSING` /
   `..._PROVENANCE_MISSING` / `..._PROVENANCE_MISMATCH` /
   `..._MANIFEST_INCOMPLETE` / `..._ARTIFACT_CHECKSUM_MISMATCH` /
   `..._CHECKPOINTS_UNCAPTURED` / `..._FINAL_IC_MISSING`) raised as
   `StateManagerError` — no snapshot write, no QC record, no index
   mutation, nonzero CLI exit.
3. **Fallback behind the gate, manifest-driven** (fixture round-2
   P1): the final-IC fallback is reachable solely when a
   gate-verified manifest declares zero checkpoints AND
   `provenance.requested_checkpoint_hours` is empty, and it
   publishes EXACTLY the file the manifest's `final_ic` entry names,
   checksum-verified — the `_find_ic_file` rglob is retired, so an
   undeclared (stale, foreign, or later-attempt-corrupted) file in
   the tree is never selected. This is the analysis lane's and
   short-horizon forecasts' normal publish path, now fully
   witnessed. A verified manifest with requested hours but zero
   captured checkpoints rejects
   (`STATE_SAVE_SOURCE_CHECKPOINTS_UNCAPTURED`) instead of
   downgrading to the fallback. The `valid_time=run.end_time`
   stamping on the fallback is legitimate exactly because the named,
   checksummed artifact is the witness-bearing solve's own final
   state (issue in-scope (d): equal-strength predicate). The final
   IC's identity is pinned at the WRITER (run-1 r3 P1): exactly two
   candidate paths (`output_dir/<project_name>.cfg.ic.update`, else
   `.cfg.ic`), no recursive search — the writer cannot bless a
   residue file under another name, and a fallback-lane manifest
   without `final_ic` rejects (`..._FINAL_IC_MISSING`).
4. **Previous-attempt trees**: a tree left by a FAILED attempt has
   no manifest (witness invariant) ⇒ rejected; its stray IC files
   additionally cannot be selected because the fallback publishes
   only the manifest-named `final_ic`. A tree left by a previous
   SUCCESSFUL solve of the SAME run_id is a legitimate publishable
   artifact of the same run identity (same cycle, same simulation
   window) — identity, not recency, is the contract; this is also
   what keeps `durable_shud_output_reused=True` retries alive.
   Recorded as an explicit deviation from the source issue's literal
   wall-clock AC-3 wording (tasks.md Deviation 2): the harmful
   population in AC-3's scenario — stale ICs from killed/partial
   attempts — is rejected via witness absence plus named-artifact
   selection plus checksum pinning.
5. **Legacy trees** (manifest without `provenance`, or pre-upgrade
   no-manifest trees): rejected
   (`STATE_SAVE_SOURCE_PROVENANCE_MISSING` / `..._MANIFEST_MISSING`).
   Writer and gate ship atomically; recovery is a forecast re-run,
   which regenerates a witness-bearing tree. No grace window (a
   grace window re-opens the #1164 hole for exactly the trees whose
   origin cannot be proven).
6. **Evidence plane**: the state CLI is DB-free on the compute node
   with no pipeline-event channel (verified: zero `pipeline_event`
   hits in `state_cli.py`/`state_manager.py`); typed reasons surface
   via the `StateManagerError` message on stderr and the nonzero
   exit, which the orchestrator's existing generic array-stage
   classification converts into a failed `state_save_qc` stage. The
   token itself lands in the Slurm task stderr/logs, not in
   candidate records (tasks.md Deviation 1).

## What Changes

1. **Writer** (`workers/shud_runtime/runtime.py`): remove the
   `write_manifest` no-targets early return (`:3090-3091`); add the
   top-level `provenance` block (incl.
   `requested_checkpoint_hours`) and `final_ic` entry. Failure lanes
   unchanged (no write).
2. **Admission gate** (`packages/common/state_cli.py`): gate G1-G5
   at the top of `save_state_for_run`; G5 integrity is judged by
   the GATE over the RAW `checkpoints` array — every shape the
   loader silently drops today (non-dict `:642-643`, missing
   fields `:646-647`, missing file `:654-655`, non-regular target
   `:658-659`) plus non-sequence `checkpoints` (`:631-633`) and
   checksum-absent entries become `MANIFEST_INCOMPLETE`, with hash
   verification on top (`_load_state_checkpoint_manifest`'s parse
   semantics unchanged); the post-gate branch publishes declared
   checkpoints, or the manifest-named `final_ic` (zero-requested
   lane), or rejects `CHECKPOINTS_UNCAPTURED` (total-miss lane);
   `_find_ic_file` is deleted (sole caller was this publish path);
   multi-root handling per design D3 (roots enumerated, first root
   passing the gate wins; a present-but-unreadable manifest and
   unsafe declared paths keep their existing hard errors).
3. **Spec delta** (`openspec/specs/cross-cycle-warm-start-chaining/
   spec.md`): 1 MODIFIED requirement (writer: manifest after every
   successful solve + provenance block; all six base scenarios
   carried in full) and 1 ADDED requirement (publish-side admission,
   fail-closed scenarios incl. the two liveness pins).
4. **No context/threading changes**: `StateRunContext`, its three
   constructors, `chain_manifests.py`, sbatch env, and the DB SELECT
   are all UNCHANGED (the round-1 P1-1/P1-2 repair eliminated the
   wall-clock field entirely).

## Impact

- Production code: `packages/common/state_cli.py`,
  `workers/shud_runtime/runtime.py` (writer only).
- Tests: `tests/test_warm_start_chaining.py`,
  `tests/test_state_manager.py` (gate anchors; existing happy paths
  gain provenance-bearing manifests), `tests/test_shud_runtime.py`
  (writer anchors), `tests/test_production_scheduler.py` (round-1
  P2-1: its state-save fixtures write provenance-less manifests and
  MUST be updated — in the regression list and evidence floor),
  `tests/test_slurm_array_contract.py` (regression only,
  `save_state_for_run` fully mocked).
- NOT changed: `run_qc` semantics; consume-side
  `check_three_way_time_consistency`; strict-warm-start retry policy
  (`scheduler_candidates.py` — its assumed guard now exists
  downstream); `_durable_shud_output_exists` bookkeeping inference
  (`scheduler_state_failure.py:63-75`, out of scope per issue);
  retention (#1307/#1318); `state_checkpoint_hours` vs
  `Update_IC_STEP` (#1317); `StateRunContext` shape.

## Behavior deltas (disclosed)

1. **The fix**: state save against a missing output root, a
   missing/legacy/foreign manifest, or an incomplete/corrupt
   checkpoint set fails with a typed `StateManagerError` and nonzero
   exit instead of publishing — the `state_save_qc` stage fails
   visibly where master minted a "clean" successor state.
2. Manifest-declared-but-missing checkpoint files: master publishes
   the surviving subset silently; post-fix the whole publish rejects
   (`STATE_SAVE_SOURCE_MANIFEST_INCOMPLETE`). Checksum-mismatched
   files likewise reject (new integrity check; master never
   verified the manifest checksums it wrote). On the fallback lane
   master published whatever `rglob().sorted()` found first;
   post-fix only the manifest-named `final_ic` is publishable — a
   differently-named stale IC in the same tree flips from
   silently-published to never-selected.
3. Run trees produced before this upgrade (no provenance / no
   manifest) are no longer state-save-able; recovery is a forecast
   re-run. Includes in-flight cycles whose forecast ran pre-merge
   and whose state_save retries post-merge.
4. `state_checkpoints.json` is now written on every successful
   solve (previously absent for zero-checkpoint-hour configs), with
   a new top-level `provenance` key; consumers ignore unknown
   top-level keys per the spec tolerance scenario (sole production
   reader is the gate itself).
5. The strict-warm-start successor retry
   (`durable_shud_output_reused=True`) now terminates in a typed
   reject when the durable output is gone, and still publishes when
   the original witness-bearing tree is intact (liveness pin A6c).
6. `.cfg.ic`-only trees (master's live `_find_ic_file` second glob,
   `state_cli.py:588-589` — reachable for any zero-requested run,
   incl. horizon <12h forecasts under the default cycle set and the
   analysis lane): master published the sorted-first find; post-fix
   these publish only via the writer's `final_ic` entry (whose
   discovery rule covers the `.cfg.ic` name), and a successful
   solve that produced NO final state at either exact path rejects
   with `STATE_SAVE_SOURCE_FINAL_IC_MISSING` instead of publishing
   an arbitrary find (run-1 r3 P1 disclosure). Retired alongside
   the rglob: `_find_ic_file`'s file-shaped-`output_uri` geometry
   (`state_cli.py:571-577`/`:702-706`) — no producer emits it, no
   test covers it (run-2 r1 P3-4 disclosure).

## Non-goals

- No run-status predicate and no wall-clock/recency predicate in
  the gate (Ruling preamble; status strings are bookkeeping, clocks
  have no sound production source — identity + witness + integrity
  is the contract).
- No `StateRunContext`/constructor/manifest-entry/env changes.
- No pipeline-event write from the compute-node CLI (Ruling 6).
- No requested-vs-captured completeness check for PARTIAL captures
  (a tree with some requested hours captured publishes its declared
  subset if retried into state_save; run-status truth stays with
  the orchestrator — design D6 known limit). The TOTAL-miss case
  does reject (`CHECKPOINTS_UNCAPTURED`, round-2 P1).
- No changes to retention, scheduler retry policy, or consume-side
  warm-start validation.
