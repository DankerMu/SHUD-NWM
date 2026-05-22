## Why

PR #190 proved a standards-based qhh full chain can run multiple GFS/IFS cycles through download, canonical conversion, forcing production, native SHUD execution on Slurm, parsing, and display-product publication. That runner is intentionally basin-specific and script-first. Production now needs the same behavior as a backend service capability for every registered runnable basin, with cycle discovery, Slurm execution, state persistence, retry/idempotency, evidence, and operations surfaces handled by the existing orchestrator, Slurm gateway, and pipeline tables.

## What Changes

- Add backend continuous-cycle scheduling for all active runnable registered Basins/SHUD model instances across GFS and IFS by default, with explicit operator filters recorded in evidence when a subset is intentionally selected.
- Promote qhh full-chain semantics into reusable model-run assembly: basin package/manifest resolution, forcing station handling, native SHUD project mode, output parsing, frequency/display publication, and partial availability.
- Run heavy cycle work through Slurm by default, using array-capable stages and compute-node reachable database/object-store preflight.
- Persist per-source/cycle/model state in `ops.pipeline_job`, `ops.pipeline_event`, `met.forecast_cycle`, `met.forcing_version`, and `hydro.hydro_run` so repeated service scans are idempotent and resumable.
- Add production evidence/reporting for cycle candidates, skipped/running/submitted/completed/failed states, Slurm accounting, forcing station counts, SHUD output rows, display product readiness, resource metrics, deterministic-vs-live execution mode, and readiness claim boundaries.

## Capabilities

### New Capabilities

- `registered-basin-cycle-discovery`
- `production-scheduler-orchestration`
- `slurm-array-runner-integration`
- `multibasin-state-idempotency`
- `runtime-evidence-and-operations`

## Impact

- `services/orchestrator/*`, `services/slurm_gateway/*`, `infra/sbatch/*`, pipeline persistence, and production validation lanes.
- Worker contracts for data adapters, canonical converter, forcing producer, SHUD runtime, output parser, and display/frequency publication.
- Backend APIs and ops monitoring may expose continuous automation state, but frontend implementation is out of scope for this change.
- qhh scripts remain useful as a diagnostic/runbook lane, but production scheduling must not depend on qhh-specific shell scripts.

## Non-Goals

- Building frontend UI for the scheduler.
- Claiming final live production readiness without target-environment receipts.
- Supporting CLDAS as a required source.
- Fabricating flood frequency curves, return periods, warning levels, station forcing, or weather data when model-specific data is unavailable.
- Re-running full live GFS/IFS multi-cycle chains in fast CI.
