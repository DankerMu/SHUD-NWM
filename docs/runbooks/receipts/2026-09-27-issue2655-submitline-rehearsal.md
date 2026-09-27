# #2655 SubmitLine exact-key fallback — node-22 scratch-journal rehearsal (2026-09-27)

Fixture task 5.4 of `openspec/changes/submitline-exact-fallback-bind`. Read-only against
production: live `sacct`/`scontrol` only, a **copy** of the production file journal, zero
`sbatch`/`scancel`, zero gateway HTTP, zero production-journal writes.

## Setup

- Code: separate clone `/scratch/frd_muziyao/nwm-2655-rehearsal` at `626f53985` plus this
  branch's diff (`git apply`), run with the active interpreter
  `/scratch/frd_muziyao/NWM/.venv/bin/python` (3.12.7); the script asserted
  `services.__file__` resolved inside the clone. The active checkout and its `.venv`
  were not touched.
- Journal: `cp -a` of `/scratch/frd_muziyao/nhms-prod/workspace/scheduler/journal`
  (383 MB) taken between scheduler passes.
- Harness: ad hoc `rehearse_2655.py` (not checked in). Fence installed before any repo
  import: subprocess limited to `sacct`, `squeue`, `scontrol show`, `sacctmgr show`;
  every non-AF_UNIX socket connect refused; filesystem writes confined to the copy and
  the output directory. It mirrors `scheduler_runtime._run_restart_reconcile`
  (reserved-unbound leg then inflight leg, `accepted_submit_grace=300s`, streak limit 3)
  with `default_comment_sacct_querier` / `default_sacct_querier`, then evaluates the
  scheduler candidate-state decision for every 0925 cohort member.
- Env: `infra/env/compute.scheduler-dbfree.env` only (the gateway secret file was not
  loaded).

## Fallback windows (live sacct with `SubmitLine`)

| reservation anchor (host-local) | eligible masters | unknown-key masters | stdout bytes |
|---|---|---|---|
| 2026-09-12T16:15:30 (ifs 2026091100) | 107 | 0 | 751 511 |
| 2026-09-13T12:34:52 (gfs 2026091212) | 100 | 0 | 686 651 |
| 2026-09-26T00:42:42 (ifs 2026092500) | 2 | 0 | 26 280 |
| 2026-09-26T00:42:55 (gfs 2026092500) | 2 | 0 | 26 280 |

Every eligible master carried a parsable `--comment=nhms_idem:<key>`, so every window
classified with basis `submitline_exact`. Byte usage stays under the 2 MiB budget.

## Run 1 — latent production blocker found

All four rows ended `journal_quarantined` /
`file_journal_reconcile_inventory_migration_invalid` (field `pipeline_jobs`); held
tuples unchanged (fail-closed). Traceback: `reconcile_reserved_unbound_jobs` ->
`commit_pipeline_job_submit_attempt` -> `_reconcile_inventory_jobs_matching_unlocked`
(fallback flat-candidate scan) -> `_iter_reconcile_direct_pipeline_job_paths`, which
rejects the non-`.json` operator residue
`pipeline-jobs/job_cycle_gfs_2026072300_convert_cohort_29a594caa8bc_forecast.json.bak-zombie-20260808`
present in the **production** journal since 2026-08-08. It was latent because no
fallback bind had ever been reached in production. Tracked separately; the rollout
moves the residue out of the journal root first (see the PR).

## Runs 2-5 — fresh copy, residue moved out of the copy

Each run is one simulated pass. The querier session's shared whole-query time budget
is consumed by the first bind's inventory scan, so the remaining rows report
`query_unavailable` / `process_unavailable` (sacct timed out; transient, held tuple
intact) and bind on a later pass — one row per pass:

| run | bound row | basis | Slurm master | inflight projection |
|---|---|---|---|---|
| 2 | ifs 2026091100 | `submitline_exact` | 47091 | `partially_failed` (36/38; expected) |
| 3 | gfs 2026091212 | `submitline_exact` | 47826 | `succeeded` |
| 4 | ifs 2026092500 | `submitline_exact` | 56823 | `succeeded` |
| 5 | gfs 2026092500 | `submitline_exact` | 56839 | `succeeded` |

Each run took ~108 s. Every bind matches the SubmitLine-proven array for that key.

## 0925 cohort members after run 5

All 94 members (47 gfs + 47 IFS): `has_active_pipeline=false`, `hydro_run` `succeeded`,
scheduler decision `skip` / `terminal_hydro_success`, `would_submit=no` — no forecast
or `state_save_qc` resubmission.

## Safety

`executed_tools = [sacct, scontrol]`, `sbatch_or_scancel_executed=false`,
`fence_violations=0` in every run.
