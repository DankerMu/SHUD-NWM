# Design: m24 Multi-basin Continuous Daemon (Live)

## Context

This change is a **live-closure / proof delta** on top of m20 and m23. The generic orchestrator is
already implemented and deterministic-tested; m24 does **not** re-specify its behavior — it
references m20/m23 contracts and adds only: node-22 gateway deployment, forecast→forecast
warm-start closure, concurrent multi-candidate dispatch with durable reservation, ≥2-basin live
proof with identity + partial-success isolation, and enforceable diagnostic-script demotion.

Code facts established by review (cited so issues do not re-discover them):
- Cohorts execute **sequentially** today: `_execute_candidates` → per source/cycle/cohort
  `orchestrate_cycle` (scheduler.py:1349); the `FileSchedulerLease` is held until `run_once` ends.
- Submission is **only** via `HttpSlurmGatewayClient` (chain.py:635); no direct-`sbatch` fallback
  in the production path. Direct `sbatch` is the QHH diagnostic runner / gateway backend only.
- Forecast runs emit selected successor checkpoint states from the same full-horizon SHUD run:
  the runtime sets `Update_IC_STEP` for restart cadence and copies timestamped T+6/T+12 snapshots
  from the overwritten `*.cfg.ic.update` while the long run continues.
- `nhms-state save` keys the snapshot `valid_time` at `hydro_run.end_time` (state_cli.py:87) and
  looks for `*.cfg.ic` (state_cli.py:104). Forecast `end_time = cycle_time + horizon` (chain.py:3696).
- SHUD writes interim restart state only to a single overwritten `*.cfg.ic.update`
  (shud.cpp:88/108, MD_update.cpp:226, IO.cpp:178); production therefore must copy required
  checkpoint states during the full forecast run instead of waiting until only the final overwrite
  survives.
- `_candidate_basin_manifest` does not emit a top-level `init_state_uri` (scheduler.py:5219); the
  cycle-stage manifest only reads `basin.get("init_state_uri")` (chain.py:2424).
- `StateSnapshot` carries only `model_id/run_id/valid_time/state_uri/checksum/usable_flag`;
  selection is by `model_id` + `valid_time <= before_time` (state_manager.py:25/340) — no lineage.
- No standalone Slurm gateway ASGI app / systemd unit is proven on node-22;
  `NHMS_SERVICE_ROLE=slurm_gateway` is reserved/fail-fast; the route is `/api/v1/slurm/health`
  (routes.py:36); real backend `health()` runs only `sinfo --version` (real_backend.py:415), not
  `sbatch/squeue/sacct/scancel`.
- The scheduler lock is a guard-file `flock` + `O_EXCL` with mtime/TTL staleness only, no
  heartbeat/renewal (scheduler.py:2577).

## Key decisions

### D1 — Warm-start mechanism: full forecast long run with opportunistic checkpoints

Operator intent: cycle N+1 (init `T_{N+1}`) initializes from cycle N's SHUD state **at `T_{N+1}`**.

The production default is **one full forecast long run** for the product horizon. T+6/T+12 states are
checkpoint side effects from that same SHUD process: runtime sets `Update_IC_STEP` to the checkpoint
cadence, watches the overwritten `*.cfg.ic.update`, copies matching header-time snapshots to
`state_checkpoints/`, and `state_save_qc` persists those checkpoints with their actual
`valid_time`. The checkpoint cadence is **not** a request to split the forecast horizon.

Short `[T_N,T_{N+1}]` reruns are allowed only as explicit manual repair for already completed
historical cycles that did not preserve checkpoint states. They are not part of the unattended
business scheduler because they waste compute and can create a different product lineage from the
published full-horizon forecast.

Required implementation pieces:
1. **Final-state normalization (HIGH)**: native SHUD writes the end state to an overwriting
   `*.cfg.ic.update` (IO.cpp:178). `nhms-state save` must prefer the captured
   `state_checkpoints/<project>.f006/f012.cfg.ic.update` manifest entries when present and
   normalize each selected checkpoint to canonical `state.cfg.ic` (recording original filename +
   target `valid_time`) before save.
2. **`Update_IC_STEP` cadence (HIGH)**: `PrintInit` only writes when `t_long % Update_IC_STEP == 0`
   (MD_update.cpp:226), default 1440 min (Model_Control.hpp:111); a 6h/12h cycle end will not hit
   the daily modulo. Forecast runtime sets `Update_IC_STEP` to the smallest requested checkpoint
   cadence while keeping `end_time == cycle_time + forecast_horizon_hours`.
3. **Consume-side filename (HIGH)**: the canonical object is `state.cfg.ic`, but SHUD reads
   `<project_name>.cfg.ic` (IO.cpp:76) and runtime clears packaged `*.cfg.ic` (runtime.py:770). The
   consuming run must materialize/rename the canonical state to `<project_name>.cfg.ic`.
4. **Long-run checkpoint capture (HIGH)**: the runtime must copy the matching T+6/T+12
   `*.cfg.ic.update` content while SHUD is still running, because the native file is overwritten by
   later restart writes.

Time-consistency acceptance (HIGH): the snapshot `valid_time`, the `.cfg.ic` header minute-time
(note `_shift_cfg_ic_time` rewrites it to run start, runtime.py:1386/1434), and the consuming run's
`start_time`/`cycle_time` must all equal `T_{N+1}`.

### D2 — Cohort warm-start wiring closure (HIGH; co-dependent with D1)

`_candidate_basin_manifest` must emit the selected state's `init_state_uri`/checksum so the cohort
basin record, the cycle-stage manifest, and the forecast runtime manifest all agree. Verified by
`test_cycle_cohort_forecast_manifest_uses_prior_cycle_saved_state`.

### D3 — Warm-start lineage (MED; schema extension required)

`StateSnapshot` and the selection predicate must be extended with source/cycle/lead, model package
version, and checksum lineage; a too-far lead or different package version yields a stable
rejection code, not silent use. Plus science-variable QC (row count vs mesh/river/lake, range/
non-negative checks, restart water-balance delta threshold for soil moisture / groundwater /
channel storage).

### D4 — Concurrency is a deliverable with durable reservation (P1; no silent de-scope)

The user requires concurrent dispatch. Today cohorts run sequentially. m24 delivers **concurrent
submit-and-return** with a **two-phase reservation protocol**: inside the lock, write a durable
candidate reservation / `pipeline_job` / idempotency key; atomically bind `slurm_job_id` on submit;
the reservation must be queryable via `candidate_state` before the lock is released, so an
overlapping pass cannot double-submit in the window. Acceptance requires a receipt showing two
candidates' submits overlapping (or not waiting for terminal). De-scoping concurrency is a separate
explicit scope decision (its own closed-as-decision issue), **not** a silent pass of this
requirement.

### D5 — Slurm gateway deployment on node-22 (P1; deployment entry must be built)

Generic chain submits only via the HTTP gateway, but no standalone gateway service is proven on
node-22. m24 must: implement/prove a standalone gateway app + systemd unit + listen URL
(`SLURM_GATEWAY_URL=http://127.0.0.1:8081`); unify the health URL to `/api/v1/slurm/health`; make
`health()` probe `sbatch/squeue/sacct/scancel` (not only `sinfo --version`); and have scheduler
preflight HTTP-probe the configured URL (not only an in-process `create_gateway().health()`).
Stale-job reconcile must use a **durable** job-id source (DB `pipeline_job`, not gateway in-memory
`_jobs`) and verify candidate identity on `sacct` reconcile. The submission contract itself is
m20/m23; m24 adds deployment + live receipts only. Demote the diagnostic direct-`sbatch` runner.

### D6 — Lock/lease on NFS needs heartbeat (P1)

The guard-file lock has no heartbeat/renewal; on NFS a TTL expiry during a long pass lets a second
process delete the lock and double-submit. m24 adds lease heartbeat/renewal; stale reclaim must
reconcile host/pid + candidate reservation + `sacct`/`squeue`. Proof requires two independent
processes on the node-22 shared `/scratch`, TTL < pass duration, real fs type, no double-submit.

### D7 — Env contract mapping (diagnostic → production)

| diagnostic (`QHH_*`)               | production (scheduler)                          |
|------------------------------------|-------------------------------------------------|
| `QHH_RUN_ROOT`                     | `WORKSPACE_ROOT` + `NHMS_SCHEDULER_RUNTIME_ROOT`|
| `QHH_CONTINUOUS_SOURCES`           | `NHMS_SCHEDULER_SOURCES`                         |
| `QHH_CONTINUOUS_MAX_CYCLES_*`      | `NHMS_SCHEDULER_MAX_CYCLES_PER_SOURCE`          |
| `QHH_CONTINUOUS_POLL_SECONDS`      | `NHMS_SCHEDULER_INTERVAL_SECONDS`               |
| `QHH_CONTINUOUS_EXECUTOR=slurm`    | `SLURM_GATEWAY_URL` + `SLURM_GATEWAY_BACKEND`   |
| `QHH_MODEL_ID`                     | `NHMS_SCHEDULER_MODEL_IDS`                       |
| (none)                             | `NHMS_SCHEDULER_LOCK_ROOT`, `..._EVIDENCE_ROOT` |

### D8 — Diagnostic retirement: enforceable guardrail (P2; dedup m20)

m20 already states production must not use QHH scripts (as discovery/runtime-evidence contract).
m24 adds only the **enforceable guardrail** (a test asserting the production scheduler/chain does
not invoke `run_qhh_cycle.sh`/`run_qhh_continuous.py`/`create_qhh_shud_manifest.py`) and the fact
that `create_qhh_shud_manifest.py` is not the production manifest builder. Drop unfalsifiable "still
works" prose; if scripts are kept, give a concrete smoke command + minimal pass condition.

### D9 — Live receipt schema (P1; field-level contract)

§0/§1/§2/§3/§4 receipts reuse the existing readiness evidence schema fields:
`schema_version/contract_id`, `run_id`, `node`, `command`, `timestamp`, `status`,
`execution_mode=live_proof`, `live_proof_accepted`, `dependency_blocker`, `artifact_refs`,
redaction/bounds, identity tuple (`source/cycle_time/model_id/basin_id/basin_version_id/
river_network_version_id`), stage/status/counts, Slurm job/accounting/log URI, published URI,
warm-start quality. Baseline lands at `artifacts/m24/<run_id>/baseline.json` with `schema_version`.

## Risks

- `.cfg.ic` valid_time vs next-cycle init-time vs header-time three-way alignment (D1) is the top
  scientific risk; the production mitigation is checkpoint capture during the full forecast long
  run, not an extra scheduled short SHUD segment.
- Concurrent submit-and-return (D4) interacts with lock/lease and stale-job reconcile; must be
  proven on node-22 `/scratch`/NFS with durable reservations, not only local `fcntl` unit tests.
- Multi-basin array proof must assert identity through retry/reindex (`original_task_id`) to rule
  out cross-talk, and prove same-name segments in different river networks are not merged.
- Gateway deployment is first live use; mitigate with mock-vs-real parity gate before live submit,
  keep diagnostic lane as bring-up rollback.

## Dependencies (single model — see Finding consistency)

m23 fresh forecast-cycle ingestion (Task 3.x, #255) is a **hard prerequisite**: it must be closed
(or explicitly BLOCKED) in its own issue **before** m24 §4 daemon live proof. m24 §4 only consumes
a closed ingestion capability; it does not implement ingestion. (Do not phrase it as
"absorbed-or-BLOCKED" elsewhere.)

- **m23**: #255 fresh ingestion (hard prereq, separate issue); Task 5.5 live Slurm / 6.6 live
  publish are subsumed by m24 §1/§4 live receipts.
- **m20 (0/33)**: scheduler-orchestration, slurm-array-runner-integration, registered-basin-cycle-
  discovery, multibasin-state-idempotency — code exists; m24 issues provide the live proof and
  m20 tasks are checked as that proof lands.
- **m2 analysis warm-start**: state_manager / `nhms-state` machinery is the basis for D1 path (b).
