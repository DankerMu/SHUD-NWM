# Project Profile: NHMS

Active profile for `codex-codeagent-workflow`. It supplements
`issue-risk-contract.md`; do not duplicate core packs/triggers here.

## Entry surfaces

- User surfaces: `apps/api`, `apps/frontend`
- Orchestration: `services/orchestrator`, `services/production_closure`
- HPC bridge: `services/slurm_gateway`, `infra/sbatch`
- Pipeline workers: `workers/data_adapters`, `canonical_converter`, `forcing_producer`
- Model workers: `model_registry`, `shud_runtime`, `output_parser`, `flood_frequency`
- Shared model state: `packages/common`, `nhms-state`
- Domain engines: `SHUD/`, `rSHUD/`, `AutoSHUD/`, `shud_omp`

## Contracts

- Production flow: ingest -> canonical -> forcing -> SHUD -> parse -> publish/display
- DB domain: PostgreSQL + PostGIS geometry + TimescaleDB hypertables
- Payloads: `pipeline_job`, `qc_result`, `run_manifest`, `run_status`
- Scientific formats: GRIB, NetCDF, Zarr, CRS/projection, shapefile sidecars
- SHUD runtime: executable, control files, IC/restart, output cadence
- HPC lifecycle: sbatch template, submit/poll/cancel/status sync
- Provider boundary: GFS, ERA5/CDS, IFS/ECMWF

## Risk axes

- Forecast-window and forcing temporal alignment
- CRS/projection, basin geometry, and raster/vector mismatch
- PostGIS/Timescale semantics, not just relational schema shape
- SHUD threading, timeout, restart compatibility, and output cadence
- Numerical stability: NaN, conservation, unit conversion drift
- Slurm mock-vs-real parity and stale cluster job reconciliation
- Manifest/QC evidence bound to the producing run and provider snapshot
- Published artifact identity across DB rows, object URIs, and frontend display

## Typical evidence

- `uv run ruff check . && uv run pytest -q`
- PostGIS/Timescale migration roundtrip on a scratch DB
- Small pipeline run through `nhms-pipeline` or seeded M1 model
- SHUD smoke run on a small example basin
- JSON-schema validation for changed pipeline evidence
- Frontend contract: `cd apps/frontend && corepack pnpm test && corepack pnpm build`

## Domain risk packs

- Geospatial / CRS / basin geometry
- Hydro-met time series / forcing windows
- SHUD numerical runtime / conservation / NaN
- PostGIS / TimescaleDB domain behavior
- Slurm production lifecycle / mock-vs-real parity
- External hydro-met providers / snapshot reproducibility
- Run manifest / QC provenance
- Published NHMS artifacts / display identity

## Domain expanded-triggers

- `NetCDF`, `GRIB`, `cfgrib`, `eccodes`, `zarr`, `xarray`
- `CRS`, `projection`, `pyproj`, `shapefile`, `geometry`, `PostGIS`
- `Timescale`, `hypertable`
- `orchestrator`, `pipeline`, `run_status`, state machine
- `Slurm`, `sbatch`, `slurm_gateway`, `production_closure`
- `SHUD`, `shud_omp`, `shud_runtime`, `restart`, `IC`, `forcing`, `meteoCov`
- `forecast window`, `GFS`, `ERA5`, `IFS`, `CDS`, `ECMWF`
- `run_manifest`, `qc_result`, `provider snapshot`
