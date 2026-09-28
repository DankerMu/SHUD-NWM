# #2675 operator-verified forcing `bind-reserved-job`: node-22 scratch-journal rehearsal (2026-09-28)

Covers fixture task 4.4 of `openspec/changes/operator-verified-forcing-bind` (design "Rollout").

Constraints against production:

- Runs against a **copy** of the production file journal; the production journal was only read (`rsync` source, read-only scans).
- Zero `sbatch` / `scancel` / `scontrol` mutations, zero gateway HTTP, zero DB connections.
- The timer was **not** stopped. The active checkout `/scratch/frd_muziyao/NWM` and its `.venv` were not touched.

## Setup

- Code: a separate clone `/scratch/frd_muziyao/nwm-2675-rehearsal`, detached at PR head `989659520`.
  - Interpreter: the active one, `/scratch/frd_muziyao/NWM/.venv/bin/python` (3.12.7), with `PYTHONPATH=<clone>`, cwd `<clone>`, `PYTHONDONTWRITEBYTECODE=1`.
  - Every harness process asserted that `services.__file__` resolves inside the clone.
- Journal snapshot: `/scratch/frd_muziyao/nwm-2675-rehearsal-out/journal`, taken the #2668 way.
  - A warm `rsync -a --delete` (10:09:19Z-10:11:40Z) overlapped a pass.
  - It was refreshed with `rsync -a --delete` at the end edge of the pass that ended 18:12:57 CST, taking 10:12:57.5Z-10:13:02.3Z. The next pass started at that same second.
  - A dry-run `rsync --itemize-changes` at 10:13:04.1Z showed **0** changed files outside `.locks`, so the copy equals the journal at the pass boundary.
  - Size: 403 MB, 36 449 files, 361 cycle segments, **95 504** jsonl records (`max_records` 100 000). No non-`.json` residue under `pipeline-jobs/`.
- Evidence: the three newest pass files were copied to `evidence-copy/`. The two synthetic passes below were added there only.
- Harness: `harness/h2675.py`, `harness/fence2675.py`, `harness/fenced_cli.py`, `harness/run.sh`, `harness/step_{before,bind,after}.sh` (ad hoc, not checked in).
  - The fence is the #2668 fence: installed before any repo import. Subprocesses are limited to `sacct`, `squeue`, `scontrol show`, `sacctmgr show`; every non-AF_UNIX `connect` is refused.
  - Filesystem writes are confined to `/scratch/frd_muziyao/nwm-2675-rehearsal-out/`.
  - `bind-reserved-job` and `list-operator-actions` ran through the real `services.orchestrator.cli` entrypoint (`runpy.run_module`, the same as `python -m`), under the fence.
  - Decisions, the completion verdict and restart reconcile come from a real `ProductionScheduler(config)` built from `infra/env/compute.scheduler-dbfree.env`, with its journal root pointed at the copy, built directly as in #2668. The gateway secret file was not loaded.
  - Its terminal stage is production's, `NHMS_ORCHESTRATOR_TERMINAL_STAGE=forecast_state_save_qc`.

## Target: a real held forcing master, rewound

Target: `job_cycle_gfs_2026092712_convert_cohort_90039372a66c_forcing`, key `cycle_gfs_2026092712_convert_cohort_90039372a66c:forcing`, 47 members, attempt 1.
Slurm ran it as array master **58740**. `sacct` shows 47/47 tasks COMPLETED, `frd_muziyao` / `friends`, and Submit `2026-09-28T12:16:42` (CST, +08:00).
Its SubmitLine is `/usr/bin/sbatch --array=0-46%15 --comment=nhms_forcing_attempt:<key>:a1 /tmp/nhms_klxmqar6.sbatch`.

**The #2675 shape happened live in production on this row.** Cycle `gfs/2026092712`, production records:

| seq | time (Z) | record |
|---|---|---|
| 21 | 04:16:09 | master `reserved`, anchor `2026-09-28T04:15:03.343576Z`, attempt comment `...:a1`, owner `frd_muziyao` / `friends`, `slurm_ownership_required` |
| 23 | 04:17:26 | master `reserved` / `submit_result_ambiguous`, no Slurm id, no `reconciliation_*` (the timeout transition) |
| 25 | 04:18:12 | `submission_ambiguous` event: `SLURM_GATEWAY_UNAVAILABLE`, "Slurm Gateway request failed: timed out" |
| 27 | 04:18:58 | **automatic forcing bind**: `submitted` / `accepted` / `slurm_controller_exact_comment` / `matched_bound`, id 58740 |
| 57 | 04:41:55 | master `succeeded` |

The automatic bind resolved the row about 92 s after the ambiguity, because the array was still visible to the controller.
The 2026-09-28T08:20Z scan in the design ("0 held forcing rows") is true for that reason. The operator path covers a held row whose array is no longer visible to the controller.

What was done, in the COPY only (all of it recorded in `results/synth.json`):

1. **Rewind, SYNTHETIC.** `journal/gfs/2026092712.jsonl` was truncated before sequence 27, the automatic bind record.
   - 26 records were kept and 241 removed; the removed suffix is saved in `rewind-removed/`. It held the rest of the cycle: both lanes' forcing, forecast and `state_save_qc` rows, 96 `hydro_run` records, 48 `array_task_reconciled` events and 8 `forecast_cycle` records.
   - Every footprint file whose sequence is at or above 27 was moved out of the copy into `rewind-removed/`: 48 `latest/gfs/2026092712/*.json`, 48 `pipeline-jobs/by-cycle/gfs/2026092712/*.json`, 6 flat `pipeline-jobs/*.json` directs, and 4 `private/runtime-root-recovery` sidecars.
   - Result, read through the repository: the master is `reserved` / `submit_result_ambiguous`, attempt 1, anchor `2026-09-28T04:15:03.343576Z`, and `forcing_submit_identity_is_complete` is true. No row of the cycle claims 58740.
2. **Hold, production-authored.** The held tuple is production's own seq-23 record; it was not synthesized.
   The harness re-applied `reconcile._transition_forcing_submit_ambiguity(repository, job)`, the pre-query write reconcile makes on every pass. It returned `wrote=0` (already held).
3. **Pass evidence, SYNTHETIC** (`evidence-copy/scheduler_2026092810_synthetic2675held.json`).
   - It copies the newest real pass and appends one `restart_reconcile.reserved_unbound.outcomes[]` entry for the master.
   - The entry is a `ReservationReconcileOutcome` with action `multiple_matches_blocked`, `match_count` 2, built as the forcing branch of `reconcile_reserved_unbound_jobs` builds it.
     It is serialized by the production `scheduler_runtime._serialize_reserved_unbound_outcome`, which overlays the durable attempt evidence, including the anchor.
   - The reserved-unbound leg was deliberately **not** run before the bind. Production is comment-less, so forcing reconcile asks the controller, and 58740 finished hours ago.
     A `global_absence` proof would take the `permit_forcing_submit_retry` exit and write `reservation_lost` into the copy, destroying the held row. That absence case is #2682 and is not under test here.

## Results

| leg | result |
|---|---|
| baseline, untouched copy | 47 × `skip` / `terminal_hydro_success`; verdict `complete`; `list-operator-actions` 0 actions, exit 0 |
| `list-operator-actions`, before | exit 1; one entry: `held_reservation_unresolved`, reason `multiple_matches_blocked`, `operator_command: bind-reserved-job`, gfs `2026-09-27T12:00:00Z`, anchor present, no `follow_up_issue` |
| decisions, before | 47 × `skip` / `active_duplicate_pipeline` (`active_status: reserved`), `held_reservations[0]` = the forcing master (`stage: forcing`, `reserved`, no Slurm id) |
| `_cycle_completion_verdict`, before | `gap` |
| negative: `--expected-attempt 2` | exit 2, `refused: stale_attempt`; journal tree sha256 unchanged (`f28c06b3…`, 35 964 files) |
| negative: SubmitLine of dg forcing master 58723 (same cycle, other key, `--array=0-0%1`) | exit 2, `refused: submitline_key_mismatch`; sha256 unchanged |
| negative: own SubmitLine with `--array=0-45%15` (46 tasks for 47 members) | exit 2, `refused: array_spec_mismatch`; sha256 unchanged |
| negative: `--slurm-user nwm` (row requires ownership, expects `frd_muziyao` / `friends`) | exit 2, `refused: slurm_owner_mismatch`; sha256 unchanged |
| **`bind-reserved-job --confirm`** | exit 0, `status: bound`, `lane: forcing`, `array_spec: 0-46%15`, `written_record_count: 2`, no warnings; **wall 54.6 s** (each refusal about 1.8-2.1 s) |
| repeat of the same bind | exit 2, `refused: not_held`; sha256 unchanged (`e37161e3…`, 35 966 files) |
| restart reconcile (real `scheduler._run_restart_reconcile`, 0.65 s) | reserved-unbound: 0 outcomes; inflight: one outcome, master 58740 `terminal` / `succeeded`, `durable_write_kind: pipeline_job_status`, 1 write |
| decisions, after (production terminal stage) | 47 × `retry` / `resume_after_completed_stage`, `restart_stage=forecast`; **no** `active_duplicate_pipeline`, no `held_reservations` |
| `_cycle_completion_verdict`, after | `gap`: forecast has not run on the copy (see below) |
| comparison only: terminal stage unset | the same 47 × `retry` / `resume_after_completed_stage` / `forecast`; verdict `gap` |
| `list-operator-actions`, after the next reconciling pass | exit 0; 0 actions (the held entry was dropped) |

Bind invocation (values from live `sacct --jobs=58740 --parsable2 -o JobID,JobName,User,Account,Submit,SubmitLine`, row `58740_0`, run under the fence):

```text
python -m services.orchestrator.cli bind-reserved-job --journal-root <copy>/journal \
  --job-id job_cycle_gfs_2026092712_convert_cohort_90039372a66c_forcing --slurm-job-id 58740 \
  --slurm-submit-time 2026-09-28T12:16:42+08:00 \
  --submit-line '/usr/bin/sbatch --array=0-46%15 --comment=nhms_forcing_attempt:cycle_gfs_2026092712_convert_cohort_90039372a66c:forcing:a1 /tmp/nhms_klxmqar6.sbatch' \
  --expected-attempt 1 --expected-attempt-started-at 2026-09-28T04:15:03.343576Z \
  --slurm-user frd_muziyao --slurm-account friends \
  --checked-by rehearsal-2675 --checked-at 2026-09-28T10:17:39Z --verification-note '<sacct summary; SYNTHETIC rewind>' --confirm
```

Receipt (stdout, `verification_note` elided):

```json
{"array_spec": "0-46%15", "checked_at": "2026-09-28T10:17:39Z", "checked_by": "rehearsal-2675", "command": "bind-reserved-job", "committed": true,
 "job_id": "job_cycle_gfs_2026092712_convert_cohort_90039372a66c_forcing", "journal_root": "/scratch/frd_muziyao/nwm-2675-rehearsal-out/journal",
 "lane": "forcing", "matched_slurm_job_id": "58740", "reconciliation_decision": "matched_bound", "reconciliation_source": "slurm_exact_comment",
 "slurm_accounting_submitted_at": "2026-09-28T04:16:42Z", "status": "bound", "status_from": "reserved", "status_to": "submitted",
 "submission_attempt": 1, "submission_attempt_started_at": "2026-09-28T04:15:03.343576Z",
 "submitline_key": "nhms_forcing_attempt:cycle_gfs_2026092712_convert_cohort_90039372a66c:forcing:a1", "warnings": [], "written_record_count": 2}
```

Durable post-state:

- The master row is `submitted` / `accepted` / `slurm_exact_comment` / `matched_bound`, with `slurm_job_id` = `matched_slurm_job_id` = 58740. Attempt and anchor are unchanged.
  - `slurm_binding_source` and `slurm_accounting_submitted_at` are both null. The receipt's `slurm_accounting_submitted_at` only echoes the verified submit input.
- **Head difference (added after merge):** this rehearsal ran at `989659520`. The merged head `a2972fca6` changes three things:
  - The forcing receipt now reports the durable `slurm_accounting_submitted_at: null` and adds `slurm_submit_time` for the verified input. At `a2972fca6`, the receipt above would show `null` and `"slurm_submit_time": "2026-09-28T04:16:42Z"`.
  - The window floors the anchor to whole seconds.
  - Owner values are validated when the command starts.
  - None of these changes a CAS outcome here. The anchor-to-Submit gap is about 99 s, so the floor has no effect, and `frd_muziyao` / `friends` pass validation. Durable post-state and refusal codes are identical.
- One `operator_verified_bind` event (sequence 28) carries:
  - `lane: forcing`, the `checked_*` fields and the note;
  - the attempt comment as `submitline_key`, `array_spec`, the Slurm id and submit time;
  - `slurm_user` / `slurm_account`, and the prior tuple (`reserved` / `submit_result_ambiguous`, empty `reconciliation_*`).
- The bind also published the row's `reconcile-inventory` anchor (`row_kind: legacy`). The legacy `_write_pipeline_job_unlocked` path, which the automatic forcing bind uses, publishes the same anchor for a non-terminal row.
  The inflight leg projected the master terminal and removed the anchor.
- After the inflight leg the master is `succeeded`. No `<id>_<n>` member-task rows were written, the same as production, where seq 57 wrote none either.

"Next reconciling pass" evidence (`evidence-copy/scheduler_2026092810_synthetic2675next.json`, SYNTHETIC):
it is the newest real pass with its `restart_reconcile` block replaced by the block `scheduler._run_restart_reconcile()` actually returned on the copy after the bind.
That block has `reserved_unbound.outcomes: []` and the inflight outcome for the master. The listing's newer-lane-ran-pass rule dropped the entry.

## Tuple parity with the automatic forcing bind

The operator-bound row, read right after the bind, was compared field by field with production's seq-27 row (the automatic bind of the same attempt), saved in `rewind-removed/`.
47 fields are identical, including `status`, `slurm_job_id`, `matched_slurm_job_id`, `submit_outcome`, `reconciliation_decision`, `reconciliation_reason_class`, `slurm_binding_source`, `slurm_accounting_submitted_at`, attempt, anchor, `slurm_comment`,
the owner fields, `cohort_members` and `candidate_projections`.
Three fields differ:

| field | automatic bind (seq 27) | operator bind |
|---|---|---|
| `reconciliation_source` | `slurm_controller_exact_comment` | `slurm_exact_comment` |
| `submitted_at` | `2026-09-28T04:18:58.221107Z` | `2026-09-28T10:18:33.599807Z` (bind time) |
| `updated_at` | `2026-09-28T04:18:58.221116Z` | `2026-09-28T10:18:33.599818Z` |

The timestamps are write times. The source difference is by design (Decision 3 pins the `bind_forcing_submit_attempt` default, `slurm_exact_comment`).
Production's automatic bind here came through the controller proof, so it carried the controller source. Both values are members of `FORCING_EXACT_COMMENT_RECONCILIATION_SOURCES` and read as a resolved forcing attempt.

## Why the cycle stays `gap` after the bind

The rewind removed everything after the forcing bind, including the cohort's forecast and `state_save_qc`.
Once bound and projected, the members leave the #2667 freeze. Their next action is the ordinary forecast resume (`restart_stage=forecast`), which a real pass would submit, and that is what the design expects.
The verdict reads `gap` until forecast (and, under production's terminal stage, `state_save_qc`) succeeds. Unsetting the terminal stage does not change this here, because forecast itself is missing.

## Observations

- **Live occurrence:** the #2675 shape (gateway timeout after Slurm accepted the forcing array) occurred in production on 2026-09-28 04:17Z and was auto-resolved by the controller match about 92 s later.
  The operator bind is the exit for the same shape once the array has left the controller.
- **Rewind fidelity, the reconcile-inventory anchor:** production removes a row's anchor when the row is terminal, and the anchor is a derived file, so the rewind could not restore the anchors the held state had.
  - The target master was not affected. The bind excludes its own job id from the claimant scan and re-published its anchor.
  - The same cycle's dg forcing master (58723, left `pending` by the cut) has no anchor in the copy, so the inventory-driven inflight leg did not project it. It stays `pending` in the copy.
    That is a rewind artifact, not a bind or reconcile defect.
- **`finished_at`:** the master projected terminal by the inflight leg keeps `finished_at: null`. Production's seq 57 carried `04:41:31Z`.
  This is the pre-existing inflight behaviour already reported in the #2668 receipt, not #2675.
- **Bind latency:** 54.6 s on this journal (#2668: 53.4 s). The refusals return in about 2 s, because every refusal fires before the inventory-locked claimant scan.
- **Budget:** the copy holds 95 504 records against `max_records` 100 000 (after the day's journal retention). The bind did not hit the record limit.

## Safety

- The `sacct` calls happened in two places:
  - Under the fence: `executed_tools = [sacct]` in exactly three processes (the bind-values query for 58740, the probe query for 58723, and the inflight leg).
  - Outside the fence, during target selection: read-only `sacct` queries for 58740, 58723 and 58414, and three read-only Python scans of the production journal and one production pass file (`/tmp/*_2675.py`, since deleted).
- `sbatch_or_scancel_executed=false`; `fence_violations=0` across all 32 fenced processes.
- Production after the run:
  - `journal/gfs/2026092712.jsonl` still has 267 records, and the production reconcile inventory holds no anchor for the master.
  - The live checkout is still at `bf114590`, with no new changes (its pre-existing untracked `.nhms-work/` is unchanged).
  - The timer is `active`.

## Paths left on node-22 (removed after merge)

Both paths below were removed on 2026-09-28 after PR #2683 merged. The list is kept as a record of what the run produced.

- Clone: `/scratch/frd_muziyao/nwm-2675-rehearsal` (detached `989659520`, clean).
- Output: `/scratch/frd_muziyao/nwm-2675-rehearsal-out/` (404 MB), which contains:
  - `journal/`: the copy, post-rehearsal.
  - `rewind-removed/`: the removed jsonl suffix (with the seq-27 automatic bind record) and the moved footprint files.
  - `evidence-copy/`: 3 real passes and 2 synthetic ones.
  - `harness/`, `results/` (every JSON above, plus `parity.json`, `row-{original,held,bound,after}.json`, `sacct-*.txt`, `bind-inputs.txt`), `fence/` (per-process fence reports), `logs/`.
  - `harness-*` scratch roots for the scheduler build.
  - `snapshot.sh` / `snapshot.log` / `snapshot.meta` (the snapshot used).
