# #2666 held reservation blocks the candidate-state decision: node-22 scratch-journal rehearsal (2026-09-27)

Covers fixture task 3.3 of `openspec/changes/held-reservation-blocks-retry`.

Constraints against production:

- Read-only apart from live `sacct` / `scontrol`.
- Runs against a **copy** of the production file journal, never the journal itself.
- Zero `sbatch` / `scancel`, zero gateway HTTP, zero production-journal writes.

## Setup

- Scheduler state: `nhms-compute-scheduler.timer` has been stopped since 2026-09-27T08:38:21Z. The last pass, `scheduler_2026092707_9e48c0d0deb0`, finished at 08:52:51Z. Timer and service were both `inactive` when the copy was taken.
- Code: a separate clone `/scratch/frd_muziyao/nwm-resubmit-rehearsal`, checked out at PR head `7aa8a591`.
  - Interpreter: the active one, `/scratch/frd_muziyao/NWM/.venv/bin/python` (3.12.7). The active checkout and its `.venv` were not touched.
  - The harness refuses to run unless `services.__file__` resolves inside the clone and the clone contains `_state_held_reservations`.
- Journal: a `cp -a` of `/scratch/frd_muziyao/nhms-prod/workspace/scheduler/journal` (377 MB).
  - It holds exactly one `reserved` row: `job_cycle_ifs_2026092512_convert_cohort_97734b86b611_forecast`, with status `reserved` / `submit_result_ambiguous`, `slurm_job_id=null`, reason class `comment_accounting_unproven`.
  - Slurm had actually run this submission as 57553 (47 tasks; task 18 FAILED with `OUTPUT_ROW_COUNT_MISMATCH` after the duplicate 57637_18 took over the run directory).
- Harness: the #2655 fenced harness, not checked in.
  - The fence confines subprocesses to `sacct`, `squeue`, `scontrol show`, and `sacctmgr show`, refuses every non-AF_UNIX socket, and confines writes to the copy and the output directory.
  - This run adds a leg that computes member decisions **before** restart reconcile, then runs reconcile and computes them again.
  - Run window: 10:22:23Z to 10:25:25Z.

## Results

| leg | IFS 0925 12Z cohort (47) | gfs 0925 12Z cohort (47) |
|---|---|---|
| before reconcile | `skip` / `active_duplicate_pipeline` ×47, `active_status: reserved` (pre-fix: `retry_after_completed_stage` ×47 = the 57637 duplicate) | bound to 57536 (`running`); a real pass judges it by the active-job path |
| restart reconcile | `bound`, `fallback_match_basis=submitline_exact`, `match_count=1`, 57553; inflight projection `partially_failed` | inflight projection `succeeded` |
| after reconcile | `skip` / `terminal_hydro_success` ×46; basin 18 (`dg_7a88e20b…`) `blocked` / `permanent_failure_guard` | `retry` / `resume_after_completed_stage`, `restart_stage=state_save_qc` ×47 (expected) |

- Fallback window: 3 eligible masters, 0 with an unknown key, 39 461 bytes of sacct stdout.
- `executed_tools = [sacct, scontrol]`, `sbatch_or_scancel_executed=false`, `fence_violations=0`.

## Rollout consequence

Basin 18's forecast actually completed under 57637 / 57687.
It is blocked only because 57553_18's failure is the freshest row once the old master binds.
Clear it with the supported exit, `scripts/node22_manual_retry_failed_runs.py` (preview first, then `--execute`).
Do this after the fix is deployed and before the timer starts.
