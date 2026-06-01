## Why

The current 22-node compute deployment is not yet business-automated: it has a running compute API and one canonical GFS cycle, but no active QHH model instance, no seeded fixed SHUD forcing stations in the live database, no produced forcing version, no real SHUD/Slurm execution path, no hydro result ingestion, and no scheduler service loop that can run from Docker without manual flags. M23 closes that gap for QHH on node 22 while preserving the M22 separation where node 27 remains a readonly display consumer of database state and published artifacts.

## What Changes

- Bootstrap the existing processed QHH Basins/SHUD project into production database state: active model instance, basin/package identity, fixed forcing stations from `qhh.tsd.forc`, and output river/segment identities.
- Turn fresh forecast cycles into a production input stream with explicit source availability, retry/fallback handling, complete canonical variable coverage, and evidence that distinguishes unavailable source data from ready data.
- Produce station forcing for each fresh cycle by interpolating/extracting canonical meteorology to the fixed SHUD forcing stations defined by the processed basin package; do not treat rSHUD/AutoSHUD as the runtime model engine.
- Replace stubbed SHUD execution with a preflighted real SHUD binary/library configuration and a working Slurm submission path from node 22.
- Parse real SHUD outputs into hydro tables and publish display-readable products, manifests, and logs under the shared published artifact root for node 27 consumption.
- Fix and operationalize the production scheduler entrypoint so the Docker compute service can run one-shot and continuous/timer modes using configured workspace/evidence roots without manual `--workspace-root` workarounds.
- Add evidence and end-to-end tests that prove download, canonical conversion, forcing, SHUD Slurm execution, parse, publish, and DB state transitions are automated for node 22 or explicitly blocked by a live dependency without false readiness claims.

## Capabilities

### New Capabilities

- `qhh-model-production-bootstrap`: imports and activates the existing QHH processed basin package and seeds fixed SHUD forcing/output identities.
- `fresh-forecast-cycle-ingestion`: discovers and downloads fresh GFS/IFS forecast cycles with complete canonical coverage and truthful source-unavailable evidence.
- `fixed-station-forcing-production`: generates per-cycle SHUD forcing from fresh canonical grids to fixed basin forcing stations using the existing forcing producer contract.
- `real-shud-slurm-execution`: validates and runs real SHUD through a node-22 Slurm path instead of `/bin/true` or diagnostic-only scripts.
- `hydro-result-ingest-and-publish`: ingests SHUD outputs into hydro/pipeline tables and publishes display-readable products, manifests, and logs.
- `compute-scheduler-operationalization`: makes the node-22 scheduler runnable from Docker/systemd with correct env defaults, service loop behavior, locks, evidence, and E2E tests.

### Modified Capabilities

None.

## Impact

- Production DB bootstrap and registry: `scripts/*qhh*`, model registry import/publish commands, `core.model_instance`, `met.met_station`, river/output segment metadata, and related validation tests.
- Forecast ingestion and forcing: GFS/IFS adapters, canonical converter/store, `workers/forcing_producer`, `met.forecast_cycle`, `met.canonical_met_product`, `met.forcing_version`, and `met.forcing_station_timeseries`.
- Runtime and Slurm: `workers/shud_runtime`, `services/slurm_gateway`, `infra/sbatch`, Slurm gateway host/service configuration, SHUD binary/library preflight, log URI publication, and runtime docs.
- Orchestration and scheduling: `services/orchestrator`, `infra/compose.compute.yml`, `infra/env/compute.example`, systemd/timer docs, locks/evidence roots, and scheduler tests.
- Parse/publish/readiness: output parser, display product publication, `hydro.hydro_run`, `hydro.river_timeseries`, `ops.pipeline_job`, `ops.pipeline_event`, published artifact manifests/logs under `/ghdc/data/nwm/published`, and 22-node E2E evidence under `artifacts/` or `/scratch/frd_muziyao`.
- Node 27 implementation is out of scope except for preserving the already-defined readonly database and published artifact contracts.

## Non-Goals

- Rebuilding the processed QHH watershed package during production cycles.
- Using rSHUD/AutoSHUD as the runtime hydrologic solver; it is a reference for SHUD input/forcing contracts.
- Coupling node 27 back to node 22 control APIs, Slurm, workspace paths, or writable compute storage.
- Claiming final production readiness without live receipts for the configured SHUD binary, Slurm path, forecast source availability, and parse/publish outputs.
- Supporting nationwide multi-basin automation beyond QHH in this closure change.
