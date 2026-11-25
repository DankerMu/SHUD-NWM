# Resource profiles gain a job-type dimension; forcing array tasks ask for one CPU

## Why

The gateway resolves a job's Slurm resources from `default` plus
`overrides[model_id]` and nothing else, so every stage of a chain asks for
what the SHUD solver needs. Measured on the compute cluster on 2026-11-25
(`sacct`, completed jobs of that day):

| job | allocated CPUs | tasks | (user+sys) / elapsed |
|---|---|---|---|
| `nhms_forcing` | 4 (12 for the two HLJ models) | 132 | 1.00 |
| `nhms_forecast` | 4 (12) | 132 | 3.5 to 4.0 |

The forcing producer is single-threaded and CPU-bound, yet each task holds
four cores (twelve for HLJ). With 80 cores, at most 20 forcing tasks run at
once; the forcing stage of one GFS+IFS pair took 70 minutes (10:12 to
11:22Z), the longest stage of the pass (#3344).

The numbers here are the measurements of 2026-11-25 on the two-host
cluster and replace those in the issue text where they differ (the issue's
90-minute baseline is from w3 alone; its 0.22 GB RSS is from nine tasks).

## What changes

- `config/resource_profiles.yaml` gains `job_type_overrides`, a sibling of
  `default` and `overrides`, keyed by the gateway job type. An entry may set
  only `cpus_per_task`, `memory_gb` and `shud_threads`.
- Resolution order becomes `default`, then `overrides[model_id]`, then
  `job_type_overrides[job_type]`, then the per-deployment overlay. The job
  type wins over the model: what a stage's program can use does not depend
  on which basin it runs for.
- One entry ships: `produce_forcing_array: {cpus_per_task: 1, shud_threads: 1}`.
- The gateway passes the job type at both places where it resolves a
  profile (array submit, single submit).

## What does not change

- `default` and every model override; the resources of
  `run_shud_forecast_array` (4 cores, 12 for HLJ) and of every job type
  without an entry.
- `resource_profile_override_model_ids` and the scheduler's cohort split:
  they keep reading `overrides` only.
- `max_concurrent`, `walltime`, `partition`, `nodes`, `ntasks` and the
  deployment overlay cannot be set per job type.
- The array concurrency bound (`NHMS_SCHEDULER_SLURM_ARRAY_CONCURRENCY_BOUND`
  and the profile's `max_concurrent`, both 32): see D5.
- `memory_gb` of forcing: see D4.

## Decisions

- **D1 The job type wins over the model.** The other order would put HLJ
  forcing back at twelve cores. A model override says how much the solver
  needs for that basin; a job-type override says what that stage's program
  can use at all.
- **D2 Keyed by job type.** It is what the gateway has at both resolve
  sites and what selects the template. `analysis_produce_forcing` is a
  different job type and is not touched.
- **D3 A closed key set for job-type entries** (`cpus_per_task`,
  `memory_gb`, `shud_threads`), refused at load otherwise. Because the job
  type wins, an entry that set `max_concurrent` or `walltime` would silently
  undo a model's own value (`m24_smoke` runs with `max_concurrent: 1`).
  An entry must set `cpus_per_task` and `shud_threads` together or neither:
  cores without threads would run a model's 4 or 12 threads on one core.
- **D4 Memory is left alone.** The cluster runs `select/cons_tres` with
  `CR_CORE` and `ConstrainRAMSpace=no` (read on w3 on 2026-11-25 from
  `scontrol show config` and `cgroup.conf`; the live receipt quotes the
  lines): memory is neither a consumable resource nor enforced, so `--mem`
  cannot limit concurrency (the worry in the issue) and lowering it changes
  nothing. Measured peak RSS of a forcing task is 0.46 GB.
- **D5 The concurrency bound stays 32 in this change.** At one CPU the
  bound, not the core count, limits forcing (30 concurrent tasks instead of
  20). Whether to raise it is decided from the live receipt, as a separate
  step with its own measurement; see "Known limits".
- **D6 Only forcing gets an entry.** `nhms_convert` is four jobs per pass
  (0.92 CPU each): nothing to gain. `nhms_state_save_qc` is waiting, not
  computing (0.08 to 0.15 CPU), and takes 229 s on w2 against 70 s on w3;
  freeing its cores would move its tasks onto the host an idle cluster fills
  first, which is the slow one. No entry until placement is settled.
  `parse_output_array` and `publish_tiles` had no jobs to measure.
- **D7 The new key is inert to old code.** `load_resource_profiles` ignores
  unknown top-level keys, so a gateway that still runs the old code keeps
  resolving exactly as before from the new file. The entry takes effect
  when the gateway process is restarted on the new code; removing the entry
  from the file is the rollback, with no restart (the file is read on every
  submission).

## Known limits

- **The gain depends on node placement.** Forcing costs 387 s of user CPU
  per task on w3 and 828 s on w2 (same CPU model; w2 runs with SMT on and is
  2.2 to 2.9 times slower on interpreter loops; host-level, tracked in
  issue 3384). It is user CPU time on both hosts, which rules out the IO
  locality reading in the issue's comment of 2026-11-23 for this stage.
  Node weights are equal and an idle cluster fills w2 first.
  At one CPU and a budget of about 30, the whole forcing stage fits on w2
  (40 CPUs as the node is defined; to be confirmed by the receipt):
  expected about 57 minutes (132 tasks / 32 x 836 s) instead of 70, not the
  25 the issue hoped for. Getting further needs one of: the w2 slowness
  fixed, w3 filled first (node weight), or a higher bound so that the stage
  spills onto both hosts. The live receipt records tasks per node and the
  stage wall so that the next step is chosen from data.
- The scheduler's candidate evidence records the model's registry profile
  as `requested`; for forcing tasks the allocated CPU count now differs from
  it. The per-task accounting in the same evidence is what Slurm did.

## Impact

- `services/slurm_gateway/resource_profiles.py`,
  `services/slurm_gateway/real_backend.py`, `config/resource_profiles.yaml`.
- Tests: `tests/test_job_array.py`, `tests/test_real_slurm_gateway.py`,
  `tests/test_production_scheduler.py` (pins only).
- Docs: `docs/runbooks/production-ops/service-bringup.md` (the profile and
  bound section).
- Spec: `job-array-orchestration` (two requirements modified).
- Deployment: the gateway on w3 must be restarted inside a deployment
  window (no pass running, no `nhms_*` job queued) for the entry to take
  effect.
