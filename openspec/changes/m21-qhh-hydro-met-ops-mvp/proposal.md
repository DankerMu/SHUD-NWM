## Why

QHH already has real GFS/IFS forcing, SHUD, parse, and display-product evidence, but the product is not yet launchable as a two-page MVP because station forcing curves are not exposed as a real API/UI path and the operations page is not fully bound to the formal pipeline/orchestrator evidence. This change narrows launch scope to QHH/limited basins and turns the existing backend, frontend, and Slurm/pipeline work into a verifiable internal MVP instead of restarting the system design.

## What Changes

- Add a real station forcing time-series API for `PRCP`, `TEMP`, `RH`, `wind`, `Rn`, and `Press` backed by `met.forcing_station_timeseries`.
- Add a QHH latest display product contract so the frontend can discover `run_id`, `forcing_version_id`, versions, source, cycle, station count, and segment count without manual IDs.
- Add or converge a two-entry MVP frontend: `/hydro-met` for QHH hydrology/meteorology display and `/ops` for operations.
- Bind the operations MVP to formal pipeline job/stage/log/retry data produced by the backend orchestrator path, not qhh-specific diagnostic scripts.
- Add smoke/readiness evidence for one QHH GFS/IFS cycle from download through display and operations controls.
- Preserve existing qhh diagnostic scripts as reproduction tools only; they are not production scheduler dependencies.

## Capabilities

### New Capabilities

- `met-station-series-api`: exposes bounded, filterable forcing station time series with provenance, units, quality flags, and truncation metadata.
- `qhh-latest-display-product`: exposes the latest QHH display product identity and availability metadata for GFS/IFS source selection.
- `hydro-met-mvp-ui`: provides the MVP hydrology/meteorology display entry with station forcing curves and river-segment `q_down` curves.
- `ops-mvp-pipeline-control`: provides the MVP operations entry for stage status, jobs, logs, Slurm metadata, and failed-run retry.
- `qhh-mvp-smoke-readiness`: defines the end-to-end QHH MVP smoke evidence and release checklist.

### Modified Capabilities

None.

## Impact

- Backend API: `apps/api/routes/data_sources.py`, likely a small MVP route/module, OpenAPI `openapi/nhms.v1.yaml`, and shared query helpers in `packages/common/forecast_store.py`.
- Database/query surface: `met.forcing_station_timeseries`, `met.forcing_version`, `hydro.hydro_run`, `hydro.river_timeseries`, `ops.pipeline_job`, and `ops.pipeline_event`; expected changes are query/index/read-path focused unless implementation discovers a missing index.
- Frontend: `apps/frontend` routes, navigation, OpenAPI-generated types, station forcing chart data flow, QHH hydrology chart data flow, monitoring/ops page convergence, and browser smoke tests.
- Orchestrator/operations: formal `nhms-pipeline plan-production` path, pipeline persistence, Slurm log URI handling, retry/cancel consistency, and controlled failure evidence.
- Documentation: MVP launch plan, progress, runbooks, and validation evidence explaining that `q_down` is the MVP hydrologic variable and `stage`, nationwide coverage, CLDAS, ERA5 near-real-time, real national MVT/PBF, and final production readiness are non-goals.

## Non-Goals

- Nationwide all-basin launch.
- Water level `stage` modeling or UI claims; MVP hydrology is river discharge `q_down`.
- CLDAS or ERA5 near-real-time as required MVP sources.
- Real national MVT/PBF production proof.
- Live IdP, live alert sink, rollback proof, or final production readiness claims.
- Replacing the formal backend scheduler/orchestrator with `scripts/run_qhh_continuous.py`.
