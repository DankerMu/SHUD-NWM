## Why

Node-22 production scheduler passes currently run 1h-2h43+ wall-clock with **zero timing instrumentation inside the planner**. All timing evidence is reverse-engineered from `sacct` / `systemctl` boundaries, which cannot distinguish python-side planning cost from Slurm queue/compute wait. This blocks any evidence-driven optimization: we cannot rank whether `restart_reconcile`, per-candidate journal reads, or per-basin `sbatch` overhead dominate a pass's addressable python-side spend.

The recent `cd952225` fix (model-scoped journal read collapse) demonstrated the risk of optimizing without data: unit tests improved 705s→57s but production passes did not shorten proportionally, because SHUD runtime and Slurm queue absorb most wall-clock. We need permanent, layered timing evidence to lock decisions on data, not intuition.

## What Changes

- Add three-layer timing instrumentation to the production scheduler:
  - **pass layer**: total wall / total CPU (via `time.process_time()` deltas at pass boundary) / python-time / slurm-wait-time / status, always on
  - **stage layer**: per (source, cycle, stage) build_candidates / dispatch / slurm_wait breakdown, always on
  - **candidate layer**: per (basin, source, stage) sub-timings around the concrete `execute_candidate_cohort` code regions (`output_uri_lookup`, `basin_manifest_build`, `slurm_env_check`, `secret_manifest_scan`, `resource_profile_check`, `stage_raw_input`, `orchestrator_dispatch`) plus, inside `chain_forecast_execution._submit_and_wait`, `build_stage_manifest`, `submit_sbatch`, `poll_until_terminal`, `post_stage_hook`. Opt-in via env
- Introduce `NHMS_SCHEDULER_TIMING_LEVEL=pass|stage|candidate` env variable; default `stage`
- Emit timing data via two surfaces:
  - existing `pass_id.json` scheduler evidence artifact gains a top-level `timing:` block (durable, comparable across passes)
  - one structured JSON line per pass/stage boundary to stdout (systemd-journald captures live progress)
- Strictly separate `python_time_ms` from `slurm_wait_ms` at every layer so SHUD/Slurm-attributable wall-clock is not conflated with addressable planner overhead
- Add `nhms-scheduler-evidence-retention.timer` systemd user unit and matching retention script; retain evidence artifacts for a bounded window (default 90 days or `NHMS_SCHEDULER_EVIDENCE_MAX_MB` cap, default 512) analogous to `nhms-node27-raw-retention.timer`

## Capabilities

### New Capabilities

- `scheduler-pass-timing-instrumentation`: three-layer wall/CPU/python-time/slurm-wait timing for every production scheduler pass, emitted to evidence JSON + structured stdout, controlled by `NHMS_SCHEDULER_TIMING_LEVEL`.
- `scheduler-evidence-retention`: bounded retention of `NHMS_SCHEDULER_EVIDENCE_ROOT` artifacts on node-22 via a systemd user timer + retention script.

### Modified Capabilities

None. `compute-scheduler-operationalization` and `continuous-daemon-live-operation` behavior is unchanged; this change only adds observability + retention.

## Impact

**Code**:
- `services/orchestrator/scheduler_runtime.py` — instrument `run_once`, stage boundaries, candidate dispatch
- `services/orchestrator/scheduler_execution.py` / `chain_forecast_execution.py` — surface per-stage python-time and slurm-wait split
- `services/orchestrator/scheduler_config.py` — read `NHMS_SCHEDULER_TIMING_LEVEL` env
- new: `services/orchestrator/scheduler_timing.py` — timing collector, JSON emission
- new: `scripts/node22_scheduler_evidence_retention.py` + `infra/systemd/nhms-scheduler-evidence-retention.{service,timer}` (sibling of the existing `infra/systemd/nhms-node27-raw-retention.{service,timer}` template; all existing units — including user-scope ones — live under `infra/systemd/`)

**Contracts**:
- Scheduler evidence JSON schema gains a `timing:` block (additive, non-breaking)
- New env variable `NHMS_SCHEDULER_TIMING_LEVEL` documented in a newly created `infra/env/compute.scheduler-dbfree.env.example` template (this change also births the template — see design D8 for rationale — using `receipts/2026-06-28-node22-dbfree-scheduler-live-proof.md` line 15/51 for the canonical on-node key set)

**Dependencies**:
- Zero runtime dependency additions (uses `time.monotonic()` + stdlib json)
- Zero impact on Slurm submission behavior, journal contract, or db-free boundary
- Retention timer independent of scheduler pass timing (can land separately if needed)

**Out of scope** (locked from grill outcomes):
- Any change to scheduler pass structure, `--continuous` semantic, or wall-clock bounds — deferred to change #2 based on timing data
- Any change to `%N` array throttle or per-basin sbatch batching — deferred to change #2
- SHUD model runtime — explicit non-goal (separate project)
