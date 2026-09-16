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
- Domain engines: `SHUD/`, `rSHUD/`, `AutoSHUD/`, `shud`
- Node-27 deployment: database container, physical PGDATA placement, user-service maintenance fences
- Display runtime configuration: `infra/env/display.env` is node-local and untracked; the display API's
  filesystem reach is declared there (`NHMS_PRECIP_MIRROR_ROOT` for the NFS canonical tree,
  `NHMS_MVT_FILE_CACHE_DIR` for the tile/PNG cache), while `apps/api/runtime_mode.py` forbids the
  compute-side roots outright. Changing that file is a deployment action with its own receipt.

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
- Service-to-service auth and OpenAPI security parity across dual-mounted control routes
- Manifest/QC evidence bound to the producing run and provider snapshot
- Published artifact identity across DB rows, object URIs, and frontend display
- SHUD required-file content integrity before package source identity/publication: bounded verified reads and zero partial object-store output
- Whole-cluster physical relocation: clean stop, complete-copy proof, exact container rebind, and stale rollback after write release
- Frontend API consumers: a new `unwrapApiData` / `getApi` consumer ships with a malformed-payload test (wrong container, null element, wrong element type).

## Typical evidence

- `uv run ruff check . && uv run pytest -q`
- PostGIS/Timescale migration roundtrip on a scratch DB
- Small pipeline run through `nhms-pipeline` or seeded M1 model
- SHUD smoke run on a small example basin
- JSON-schema validation for changed pipeline evidence
- Frontend contract: `cd apps/frontend && corepack pnpm test && corepack pnpm build`
- Live data/display receipts for active DB, ingest, display API, frontend, and
  cross-plane identity run on node-27.
- Slurm scheduling/runtime receipts run on node-22 only when sbatch, Slurm
  gateway, SHUD compute, or scheduler behavior changes.
- Local lint/unit/OpenSpec checks are necessary but do not substitute for
  required node-27 live DB/display receipts.

## Command entry points

- Setup: `uv sync --all-extras --dev`; frontend `CI=true corepack pnpm install --frozen-lockfile`.
- Backend: `uv run ruff check .`; `uv run pytest -q`; focused `uv run pytest -q tests/<file>.py`.
- Contracts: `openspec validate <change> --strict --no-interactive`; JSON schemas use the `check-jsonschema` loop in `.github/workflows/ci.yml`.
- Frontend: `cd apps/frontend && corepack pnpm test && corepack pnpm build`.
- Node-27 display receipts: `scripts/node27_river_tile_coordinate_evidence.py` (tile coordinate budget
  straight from `postgis_tile_sql`, read-only) and `scripts/node27_display_v2_browser_evidence.mjs`
  (live browser layout/overlay oracle, zero mocks); mirror gaps follow
  `docs/runbooks/canonical-precip-mirror-backfill.md`.

## Verification matrix

- Python/shared helper -> focused pytest + `uv run ruff check .` -> passing tests and zero lint findings.
- Auth/RBAC + OpenAPI security -> focused policy/route/no-side-effect tests + runtime/static drift check -> enforcement and documented security sets match with no secret material.
- JSON Schema/examples -> CI `check-jsonschema` metaschema/example loop -> every example validates against its named schema.
- OpenSpec -> `openspec validate <change> --strict --no-interactive` -> strict-valid change.
- DB migration/Timescale behavior -> node-27 isolated real-DB pytest/catalog query -> captured exact-head evidence; never run migration tests against production `nhms`. Live host PGDATA is `/data/GHDC/nhms-primary/pgdata` (container `/home/postgres/pgdata/data`), per operator update 2026-09-12. Production activation is a separate maintenance-window action.
- Physical PGDATA tooling -> exact-image disposable node-27 copy/rebind/rollback oracle; actual production hardware/performance and cutover remain separately authorized live gates
- Working-set capacity/receipt -> node-27 focused CLI/schema/history/alert tests plus an isolated-checkout read-only live audit -> configured PGDATA path/device/free bytes govern comparison; missing evidence is non-healthy; old receipts remain readable without current-writer home fallback.
- Display/API/frontend -> node-27 live receipt + frontend test/build -> C1-C4 receipt and passing build.
- Slurm/SHUD scheduling -> node-22 runtime receipt -> terminal Slurm/SHUD evidence; only when scheduling/runtime changes.

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
- `SHUD`, `shud`, `shud_runtime`, `restart`, `IC`, `forcing`, `meteoCov`
- `forecast window`, `GFS`, `ERA5`, `IFS`, `CDS`, `ECMWF`
- `run_manifest`, `qc_result`, `provider snapshot`
- `.sp.riv`, `.sp.rivseg`, `iRiv`, `source_identity`, `BASINS_RIVSEG_MAPPING_*`
