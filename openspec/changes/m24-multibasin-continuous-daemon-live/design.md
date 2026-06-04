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
- `state_save_qc` is in **ANALYSIS_STAGES** (chain.py:198), **not** forecast `M3_STAGES` (which
  ends at `publish`, chain.py:130–182). Forecast runs do not currently save a successor state.
- `nhms-state save` keys the snapshot `valid_time` at `hydro_run.end_time` (state_cli.py:87) and
  looks for `*.cfg.ic` (state_cli.py:104). Forecast `end_time = cycle_time + horizon` (chain.py:3696).
- SHUD writes interim restart state only to a single overwritten `*.cfg.ic.update`
  (shud.cpp:88/108, MD_update.cpp:226, IO.cpp:178); the end-of-run state is what survives.
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

### D1 — Warm-start mechanism: short analysis/nowcast segment is the default (HIGH; 3-reviewer consensus)

Operator intent: cycle N+1 (init `T_{N+1}`) initializes from cycle N's SHUD state **at `T_{N+1}`**.

Path (a) "SHUD restart cadence" is **rejected as default**: SHUD's `PrintInit` writes only one
overwritten `*.cfg.ic.update`, so a 7-day forecast cannot reliably yield a savable `T_{N+1}` mid-
state, and `state_save` does not even collect `*.cfg.ic.update`. (a) is allowed only if a future
change adds timestamped, non-overwriting restart IC artifacts + a `state_save` that selects by
target valid_time + header-time validation.

Path (b) — **short analysis/nowcast segment `[T_N, T_{N+1}]`** — is the default. The **time
semantics** are supported today (`AnalysisRunContext.end_time` can be set to `T_{N+1}` chain.py:3955,
`nhms-state save` keys the snapshot at `end_time`, `state_save_qc` is in ANALYSIS_STAGES), but path
(b) is **not yet end-to-end functional**: m24 must add the four implementation pieces below before
it persists a usable `T_{N+1}` IC. The forecast for products still runs the full horizon; the
analysis segment exists only to produce the IC for the next cycle. Cost: one short extra SHUD run
per cycle.

Required implementation pieces (do not claim "supported today"):
1. **Final-state normalization (HIGH)**: native SHUD writes the end state to an overwriting
   `*.cfg.ic.update` (IO.cpp:178), but `nhms-state save` only finds `*.cfg.ic` (state_cli.py:104).
   m24 must normalize the run artifact to a canonical `state.cfg.ic` (recording original filename +
   target `valid_time`) before save.
2. **`Update_IC_STEP` cadence (HIGH)**: `PrintInit` only writes when `t_long % Update_IC_STEP == 0`
   (MD_update.cpp:226), default 1440 min (Model_Control.hpp:111); a 6h/12h cycle end will not hit
   the daily modulo, and runtime does not set it today (runtime.py:389). The analysis segment must
   set `Update_IC_STEP` to a cadence that lands on `T_{N+1}`.
3. **Consume-side filename (HIGH)**: the canonical object is `state.cfg.ic`, but SHUD reads
   `<project_name>.cfg.ic` (IO.cpp:76) and runtime clears packaged `*.cfg.ic` (runtime.py:770). The
   consuming run must materialize/rename the canonical state to `<project_name>.cfg.ic`.
4. **Causal forcing policy (HIGH)**: the analysis pipeline fixes `ANALYSIS_SOURCE_ID=ERA5`
   (chain.py:57), downloads a whole UTC day, and ERA5 truncates the cycle to 00Z
   (era5_adapter.py:1019) — wrong for a real-time 6h cycle. path(b) must define a causal,
   no-future-leak `[T_N, T_{N+1}]` forcing policy (e.g. cycle N's `0..Δ` forecast lead or
   best-available nowcast); ERA5 is allowed only in delayed reanalysis mode.

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
  scientific risk; the extra analysis segment per cycle is the cost of correctness.
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
