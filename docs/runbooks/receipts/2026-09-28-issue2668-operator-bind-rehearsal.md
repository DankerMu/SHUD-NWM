# #2668 operator-verified `bind-reserved-job`: node-22 scratch-journal rehearsal (2026-09-28)

Covers fixture task 5.4 of `openspec/changes/operator-verified-reserved-bind` (design "Rollout").

Constraints against production:

- Runs against a **copy** of the production file journal; the production journal was only read (`cp -a` / `rsync` source).
- Zero `sbatch` / `scancel` / `scontrol` mutations, zero gateway HTTP, zero DB connections.
- The timer was **not** stopped. The active checkout `/scratch/frd_muziyao/NWM` and its `.venv` were not touched.

## Setup

- Code: a separate clone `/scratch/frd_muziyao/nwm-2668-rehearsal`, detached at PR head `f7f024bfc`.
  - Interpreter: the active one, `/scratch/frd_muziyao/NWM/.venv/bin/python` (3.12.7), with `PYTHONPATH=<clone>`, cwd `<clone>`, `PYTHONDONTWRITEBYTECODE=1`.
  - Every harness process asserted that `services.__file__` resolves inside the clone.
- Journal snapshot: `/scratch/frd_muziyao/nwm-2668-rehearsal-out/journal`.
  - Passes now run back to back: `OnUnitActiveSec=5min` is shorter than a pass, so the idle gap between passes was 0-18 s. A 2-minute `cp -a` cannot fit in it.
  - The first `cp -a` (03:02:10Z-03:04:20Z) therefore overlapped a pass. It was refreshed with `rsync -a --delete` at the end edge of pass `scheduler_2026092803_e83b72d66e6d` (11:13:07 CST), taking 03:13:07.7Z-03:13:12.4Z.
  - A dry-run `rsync --itemize-changes` at 03:13:14.0Z showed **0** changed files outside `.locks`, so the copy equals the journal at the pass boundary.
  - Size: 398 MB, 363 cycle segments, **96 870** jsonl records. No non-`.json` residue under `pipeline-jobs/`.
- Evidence: the three newest real pass files were copied to `evidence-copy/`. The two synthetic passes below were added there only.
- Harness: `harness/h2668.py`, `harness/fence2668.py`, `harness/fenced_cli.py`, `harness/run.sh` (ad hoc, not checked in).
  - The fence is installed before any repo import. Subprocesses are limited to `sacct`, `squeue`, `scontrol show`, `sacctmgr show`; every non-AF_UNIX `connect` is refused.
  - Filesystem writes (`open`, `os.open`, rename/replace/unlink/mkdir, ...) are confined to `/scratch/frd_muziyao/nwm-2668-rehearsal-out/`.
  - `bind-reserved-job` and `list-operator-actions` ran through the real `services.orchestrator.cli` entrypoint (`runpy.run_module`, the same as `python -m`), under the fence.
  - Decisions and the completion verdict come from a real `ProductionScheduler(config)` built from `infra/env/compute.scheduler-dbfree.env`, with its journal root pointed at the copy. The gateway secret file was not loaded.
  - The scheduler is built directly rather than through `from_env`: the harness-local runtime roots fail the runtime-root preflight, which is not under test. Registry, adapters, and journal are the same.
  - Its terminal stage is production's, `NHMS_ORCHESTRATOR_TERMINAL_STAGE=forecast_state_save_qc`.

## Held row: SYNTHETIC

Production has no held row, so one was synthesized on a completed current-contract cohort.
Target: `job_cycle_gfs_2026092600_convert_cohort_5714fbc21b79_forecast`, key `cycle_gfs_2026092600_convert_cohort_5714fbc21b79:forecast`, 47 members.
In production, Slurm ran it as array master **57935**: `sacct` shows 47/47 tasks COMPLETED, Submit `2026-09-27T22:52:08` (CST, +08:00), and a SubmitLine carrying `--comment=nhms_idem:<that key>`.

**Deviation from the design Rollout ("the legacy upsert path the #2667 tests used"):** that path cannot produce this shape on a real completed row.

- `upsert_pipeline_job` compares a current-contract master's persisted state after the merge. It refuses to turn a `succeeded` master back into `reserved` (`file_journal_evidence_invariant_invalid`).
- The #2667 tests build their held rows with a fresh `orchestrate_cycle`, not by upserting.
- The cycle also holds 47 member-task rows `job_fcst_..._forecast_reconciled_57935_<n>` (`slurm_job_id` `57935_<n>`).
  The bind's same-cycle strict exclusivity would correctly refuse those as `slurm_id_claimed`.
  A real held master never has them, because they are written by the inflight projection, which runs only after binding.

What was done instead, in the COPY only. This is hand-editing plus the typed writers, and all of it is recorded in `results/synth.json`:

1. **Rewind** `gfs/2026092600` to the state immediately before the real bind.
   - `journal/gfs/2026092600.jsonl` was truncated before sequence 109, the master's `pending 57935` record (14:52:10Z). 108 records were kept and 159 removed; the removed suffix is saved in `rewind-removed/`.
   - Every footprint file whose sequence is at or above 109 was moved out of the copy into `rewind-removed/`: 48 `latest/gfs/2026092600/*.json`, 47 `pipeline-jobs/by-cycle/gfs/2026092600/*_reconciled_57935_*.json`, and the cohort `forecast` and `state_save_qc` direct rows.
   - Result, read through the repository: the master is `reserved`, attempt 1, anchor `2026-09-27T14:51:03.674913Z`. All 47 members have hydro `created`. No row of the cycle claims 57935.
2. **Hold** through the production typed writers:
   - `transition_pipeline_job_submit_evidence(AcceptedSubmitTransition.timeout(), CAS attempt 1, reserved, unbound)`. This is the gateway-timeout writer of `chain_stage_execution`.
   - A `submission_ambiguous` event, marked `synthetic_rehearsal`.
   - `AcceptedSubmitTransition.accounting("accounting_unavailable", submit_outcome="submit_result_ambiguous", reconciliation_reason_class="comment_accounting_unproven")`.
     This is the `reconcile._record_file_reconciliation` tuple, the same call `_record_reconcile_outcome` makes in `tests/test_scheduler_held_reservation_block.py`.
   - Result: the exact held tuple `reserved` / `submit_result_ambiguous` / `slurm_exact_comment` / `accounting_unavailable` / `comment_accounting_unproven`, with `slurm_job_id` and `matched_slurm_job_id` null and the reconcile-inventory anchor present.
3. **Pass evidence, SYNTHETIC** (`evidence-copy/scheduler_2026092803_synthetic2668held.json`).
   - It copies the newest real pass and appends one `restart_reconcile.reserved_unbound.outcomes[]` entry for the master: action `ambiguous_fallback_match`, basis `name_window_count`, `match_count` 2, and `submission_attempt_started_at` taken from the durable row.
   - The reserved-unbound leg was deliberately not run before the bind. With live `sacct` exposing the SubmitLine, the #2655 `submitline_exact` fallback would presumably bind this row automatically (not exercised). So the (a)-shape outcome is synthetic, not reconcile output.

## Results

| leg | result |
|---|---|
| baseline, untouched copy | 47 × `skip` / `terminal_hydro_success`; verdict `complete`; `list-operator-actions` 0 actions, exit 0 |
| `list-operator-actions`, before | exit 1; one entry: `held_reservation_unresolved`, reason `ambiguous_fallback_match`, `operator_command: bind-reserved-job`, `source_id` gfs, `cycle_time` 2026-09-26T00:00:00Z, anchor present |
| decisions, before | 47 × `skip` / `active_duplicate_pipeline` (`active_status: reserved`, hydro `created`), the #2667 freeze |
| `_cycle_completion_verdict`, before | `gap` |
| negative: `--expected-attempt 2` | exit 2, `refused: stale_attempt`; journal tree sha256 unchanged (`46a8a9eb…`, 35 806 files) |
| negative: SubmitLine of dg master 57874 (same cycle, other key) | exit 2, `refused: submitline_key_mismatch`; sha256 unchanged |
| **`bind-reserved-job --confirm`** | exit 0, `status: bound`, `written_record_count: 2`, no warnings; **wall 53.4 s** (the refusals take about 2 s) |
| repeat of the same bind | exit 2, `refused: not_held`; sha256 unchanged (`b93ae81b…`) |
| restart reconcile (real `scheduler._run_restart_reconcile`, 3.0 s) | reserved-unbound: 0 outcomes; inflight: master `terminal` / `succeeded`, 47/47 tasks `succeeded`, 47 member-task rows `57935_<n>` written |
| decisions, after (production terminal stage) | 47 × `retry` / `resume_after_completed_stage`, `restart_stage=state_save_qc`, hydro `succeeded`; **no** `active_duplicate_pipeline` |
| `_cycle_completion_verdict`, after (production terminal stage) | `gap`: the cohort's `state_save_qc` has not run on the copy (see below) |
| comparison only: terminal stage unset (the lane-test setting) | 47 × `skip` / `terminal_hydro_success`; verdict `complete` |
| `list-operator-actions`, after the next reconciling pass | exit 0; 0 actions (the held entry was dropped) |

Bind invocation (values from live `sacct -j 57935 -X -P -o JobID,JobName,State,Submit,SubmitLine,User,Account`):

```text
python -m services.orchestrator.cli bind-reserved-job --journal-root <copy>/journal \
  --job-id job_cycle_gfs_2026092600_convert_cohort_5714fbc21b79_forecast --slurm-job-id 57935 \
  --slurm-submit-time 2026-09-27T22:52:08+08:00 \
  --submit-line '/usr/bin/sbatch --array=0-46%15 --comment=nhms_idem:cycle_gfs_2026092600_convert_cohort_5714fbc21b79:forecast /tmp/nhms_9a3umbzp.sbatch' \
  --expected-attempt 1 --expected-attempt-started-at 2026-09-27T14:51:03.674913Z \
  --checked-by rehearsal-2668 --checked-at 2026-09-28T03:17:42Z --verification-note '<sacct summary; SYNTHETIC>' --confirm
```

Receipt (stdout, `verification_note` elided):

```json
{"checked_at": "2026-09-28T03:17:42Z", "checked_by": "rehearsal-2668", "command": "bind-reserved-job", "committed": true,
 "job_id": "job_cycle_gfs_2026092600_convert_cohort_5714fbc21b79_forecast",
 "journal_root": "/scratch/frd_muziyao/nwm-2668-rehearsal-out/journal", "matched_slurm_job_id": "57935",
 "reconciliation_decision": "matched_bound", "reconciliation_source": "slurm_name_window_unique",
 "slurm_accounting_submitted_at": "2026-09-27T14:52:08Z", "status": "bound", "status_from": "reserved", "status_to": "submitted",
 "submission_attempt": 1, "submission_attempt_started_at": "2026-09-27T14:51:03.674913Z",
 "submitline_key": "nhms_idem:cycle_gfs_2026092600_convert_cohort_5714fbc21b79:forecast", "warnings": [], "written_record_count": 2}
```

Durable post-state:

- The master row is `submitted` / `accepted` / `slurm_name_window_unique` / `matched_bound`, with `slurm_binding_source=slurm_name_window_unique` and `slurm_accounting_submitted_at=2026-09-27T14:52:08Z`. Its attempt and anchor are unchanged.
- One `operator_verified_bind` event (sequence 113) carries the `checked_*` fields, the note, the SubmitLine key, the Slurm id, the submit time, and the prior held tuple.
- After the inflight leg the master is `succeeded`.

"Next reconciling pass" evidence (`evidence-copy/scheduler_2026092803_synthetic2668next.json`, SYNTHETIC):

- It is the newest real pass with its `restart_reconcile` block replaced by the block `scheduler._run_restart_reconcile()` actually returned on the copy after the bind: `reserved_unbound.outcomes: []`, and the inflight outcome for the master.
- The listing drops an entry when a newer lane-ran pass no longer reports that job held. That rule dropped the entry here.

## Why the cycle stays `gap` after the bind

Production runs with `NHMS_ORCHESTRATOR_TERMINAL_STAGE=forecast_state_save_qc`.
Once bound and projected, the members leave the #2667 freeze. Their next action is the ordinary `state_save_qc` resume, which a real pass would submit.
The rewind removed the cohort's historical `state_save_qc` (it ran after the forecast bind), so the verdict reads `gap` until that stage succeeds.
Under the lane test's setting (terminal stage unset) the same copy reads 47 × `terminal_hydro_success` and `complete`.
The design's "cycle complete" expectation therefore holds for the forecast terminal stage. On production it shows up as "released to the next stage".

## Budget facts

- The bind succeeded on the production-sized copy: 363 segments and 96 870 jsonl records, against `max_records` 100 000.
- For comparison, a whole-tree replay of the same copy (`_iter_pipeline_job_records()`) raised `file_journal_record_limit_exceeded` after 124 s.
  - So the bind's claimant scan (same-cycle replay plus the reconcile-inventory scan) did not replay the whole tree.
- Wall times: the bind took 53.4 s end to end, including about 2 s of interpreter start, the same as each refusal. Restart reconcile took 3.0 s. The synth typed writes took about 30 s in total.

## Observations

- **Snapshot discipline:** with passes back to back, "copy while the service is inactive" is not achievable without stopping the timer. An edge-triggered `rsync` refresh plus a dry-run proof that nothing changed is what gave a consistent copy.
- **`finished_at`:** the master projected terminal by the restart-reconcile inflight leg keeps `finished_at: null`. The same master projected in production carried `2026-09-27T15:32:36Z`.
  - The inflight `sacct` format carries no End field. This is pre-existing inflight-reconcile behaviour, not #2668. It is reported, not fixed.
- **Bind latency:** a bind takes about a minute on this journal. The runbook should not treat a slow bind as hung.

## Safety

- The `sacct` calls happened in two places:
  - Under the fence: `executed_tools = [sacct]` (the bind-values query and the inflight leg).
  - Outside the fence: one read-only `sacct -j 57874 -X -P -n -o SubmitLine` from the driver shell, to fetch the negative-probe SubmitLine.
- `sbatch_or_scancel_executed=false`; `fence_violations=0` across all 25 fenced processes.
- Production after the run:
  - `journal/gfs/2026092600.jsonl` still has 267 records, and the production reconcile inventory holds no anchor for it.
  - The live checkout is still at `56f2f745`, with no new changes.
  - The timer is `active`.

## Paths left on node-22 (for cleanup after merge)

- Clone: `/scratch/frd_muziyao/nwm-2668-rehearsal` (detached `f7f024bfc`, clean).
- Output: `/scratch/frd_muziyao/nwm-2668-rehearsal-out/`, which contains:
  - `journal/`: the copy, post-rehearsal.
  - `rewind-removed/`: the removed jsonl suffix and footprint files.
  - `evidence-copy/`: 3 real passes and 2 synthetic ones.
  - `harness/`, `results/` (every JSON above), `fence/` (per-process fence reports).
  - `copy_journal.sh` / `copy.meta` (first, overlapping copy), `sync_journal.sh` / `sync.log` (idle-gap attempt, abandoned), `sync2.sh` / `sync2.log` / `snapshot.meta` (the snapshot used).
