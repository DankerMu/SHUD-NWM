## Change Surface

- `workers/data_adapters/gfs_adapter.py`
  - `GFSAdapterConfig.cycle_hours_utc` default factory / env parsing.
- `workers/data_adapters/ifs_adapter.py`
  - `IFSAdapterConfig.cycle_hours_utc` default factory / env parsing.
- Shared helper location if implementation avoids duplication.
- `infra/env/compute.example`
  - scheduler allowed hours, adapter cycle-hour env, strict warm-start env, and
    cycle-lag comments.
- `docs/runbooks/current-production-ops.md` and/or adjacent runbooks.
- Tests:
  - `tests/test_gfs_adapter.py`
  - `tests/test_ifs_adapter.py`
  - `tests/test_production_scheduler.py` where hard-gate compatibility evidence
    is already nearby.

## Must Preserve

- Scheduler allowed-cycle hard gate remains the authoritative business execution
  boundary and still rejects `06/18` even if an adapter or fake adapter returns
  them.
- Existing default adapter behavior remains `00/06/12/18` unless env explicitly
  narrows it; tests and bootstrap lanes can still configure four cycles.
- Adapter availability evidence and `as_data_source_config()` continue to expose
  the active cycle-hour list.
- IFS lead-time policy for `06/18` remains available when explicitly configured.
- `published/` remains display-only; object-store copyback remains the location
  for `runs/` and `forcing/` packages.

## Must Add / Change

- Env parsing:
  - `GFS_CYCLE_HOURS_UTC` configures `GFSAdapterConfig.cycle_hours_utc`.
  - `IFS_CYCLE_HOURS_UTC` configures `IFSAdapterConfig.cycle_hours_utc`.
  - Parsing uses scheduler-compatible semantics: comma-separated integer UTC
    hours, trim whitespace, reject empty tokens, reject non-integers, reject
    booleans/direct non-int config values where applicable, enforce `0..23`,
    dedupe and sort.
  - Invalid env fails fast during config construction.
- Production env example:
  - includes `NHMS_SCHEDULER_ALLOWED_CYCLE_HOURS_UTC=0,12`.
  - includes `GFS_CYCLE_HOURS_UTC=0,12` and `IFS_CYCLE_HOURS_UTC=0,12`.
  - includes `NHMS_REQUIRE_FORECAST_WARM_START=true`.
  - explains `NHMS_SCHEDULER_CYCLE_LAG_HOURS` floors discovery to the prior
    allowed-cycle boundary; adapter cycle hours only reduce provider probes.
- Runbook:
  - states node-27 reads forcing from the shared object-store `forcing/...`
    prefix, not `published/`, and provides explicit node-22/node-27 mount
    examples for `forcing/` and `runs/`.
  - states `published/` contains tiles/logs/display manifests only.
  - documents strict warm-start failure handling and checks for prior
    `state_save_qc`/state snapshot.
  - provides SQL/shell checks for forcing package manifest, run output, state
    snapshot, scheduler evidence, and published display artifacts.

## Risk Packs Considered

- Public API / CLI / script entry: selected - adapter config affects CLI/sbatch
  adapter construction and production scheduler docs.
- Config / project setup: selected - new adapter env keys and production env
  comments.
- File IO / path safety / overwrite: selected - docs distinguish object-store,
  shared copyback, and published paths; no new file writes are expected.
- Schema / columns / units / field names: selected - cycle-hour fields and env
  names must be stable and scheduler-compatible.
- Auth / permissions / secrets: not selected - no credential handling changes.
- Concurrency / shared state / ordering: selected - adapter probe narrowing must
  not replace scheduler hard gate or strict warm-start ordering.
- Resource limits / large input / discovery: selected - narrowing probes reduces
  external discovery breadth; invalid config fails fast.
- Legacy compatibility / examples: selected - default four-cycle adapter behavior
  and explicit four-cycle tests remain compatible.
- Error handling / rollback / partial outputs: selected - invalid env parsing
  should be stable fail-fast; docs specify strict failure handling.
- Release / packaging / dependency compatibility: not selected - no dependency
  change.
- Documentation / migration notes: selected - production runbook/env update is a
  primary deliverable.
- Geospatial / CRS / basin geometry: not selected - no geometry/CRS change.
- Hydro-met time series / forcing windows: selected - cycle-hour selection
  changes which provider cycles are probed.
- SHUD numerical runtime / conservation / NaN: not selected - no SHUD runtime
  numerical change.
- PostGIS / TimescaleDB domain behavior: not selected - no DB schema/query
  behavior change.
- Slurm production lifecycle / mock-vs-real parity: selected - docs and env
  affect production scheduler/Slurm runbooks.
- External hydro-met providers / snapshot reproducibility: selected - GFS/IFS
  discovery/probe behavior changes under env.
- Run manifest / QC provenance: selected - runbook must bind forcing/run/state
  checks to the correct object-store and scheduler evidence locations.
- Published NHMS artifacts / display identity: selected - published vs
  object-store boundary is explicitly corrected.

## Invariant Matrix

Governing invariant: Production business discovery may reduce provider probes to
UTC `00/12`, but only the scheduler hard gate may decide executable business
cycles, and operator docs must point each artifact check at its true source of
truth.

Source-of-truth identity/contract: `GFS_CYCLE_HOURS_UTC`,
`IFS_CYCLE_HOURS_UTC`, `NHMS_SCHEDULER_ALLOWED_CYCLE_HOURS_UTC`,
`NHMS_REQUIRE_FORECAST_WARM_START`, `OBJECT_STORE_ROOT`,
`NHMS_OBJECT_STORE_COPYBACK_ROOT`, `NHMS_PUBLISHED_ARTIFACT_ROOT`, source id,
cycle time, run id, forcing package URI, state snapshot URI, and scheduler
evidence JSON.

Surfaces:

- Producers: GFS/IFS adapter discovery and provider probe URI generation.
- Validators/preflight: adapter env parser and scheduler allowed-cycle parser.
- Storage/cache/query: object-store `forcing/`, object-store `runs/`, DB
  `met.forcing_version`, `hydro.state_snapshot`, `ops.pipeline_job`.
- Public routes/entrypoints: adapter config construction, scheduler
  `plan-production`, operator runbooks.
- Frontend/downstream consumers: node-27 read-only display consumes DB,
  shared object-store, and published display artifacts.
- Failure paths/rollback/stale state: invalid env fail-fast; strict warm-start
  missing checkpoint blocks next forecast until prior state save is repaired.
- Evidence/audit/readiness: scheduler evidence, forcing package manifest,
  run output, state snapshot rows/files, published display logs/tiles.

Regression rows:

- `GFS_CYCLE_HOURS_UTC=0,12` -> GFS discovery/probe only emits `00/12`.
- `IFS_CYCLE_HOURS_UTC=0,12` -> IFS discovery/probe only emits `00/12`.
- Env `12,0,12` -> adapter cycle hours normalize to `(0, 12)`.
- Direct config `cycle_hours_utc=(12, 0, 12)` -> adapter cycle hours normalize to
  `(0, 12)`.
- Env `0,,12`, `abc`, `24`, `-1`, or blank -> config construction fails fast.
- Direct config `cycle_hours_utc=(True,)`, `("12",)`, or `(12.5,)` -> config
  construction fails fast with `ValueError`.
- No adapter env -> legacy default `(0, 6, 12, 18)` remains.
- Scheduler configured `0,12` still rejects a fake/adapter-produced `06/18` as
  `cycle_hour_not_allowed` before candidates/readiness/forcing/submit.
- Production env example shows scheduler allowed hours, adapter hours, and strict
  warm-start together.
- Runbook checks forcing by normalizing relative or configured-prefix package
  URIs to node-22/node-27 shared object-store `forcing/...`, run output under
  node-22/node-27 shared object-store `runs/...`, and display artifacts under
  `published/...` without implying full business products live in `published`.
- Runbook strict warm-start failure path verifies the exact successor
  checkpoint at current `cycle_time` with `lead_hours=12`, then directs
  operators to producer `state_save_qc` / state snapshot repair, not cold start.

## Boundary-Surface Checklist

- Shared helper roots: adapter/scheduler cycle-hour parsing should not drift.
- Public entrypoints: adapter config default construction and CLI/sbatch use.
- Read surfaces: provider availability probes and operator verification commands.
- Write/delete/overwrite surfaces: docs only for paths; no new write/delete code.
- Producer/consumer evidence boundaries: forcing/run/state/published artifacts
  must be checked at their actual source-of-truth paths.
- Unchanged downstream consumers: scheduler hard gate, strict warm-start, and
  four-cycle explicit compatibility tests.
