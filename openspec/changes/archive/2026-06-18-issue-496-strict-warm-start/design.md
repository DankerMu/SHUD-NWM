## Change Surface

- `services/orchestrator/chain.py`
  - `OrchestratorConfig` env construction.
  - `_select_forecast_initial_state()` exact/latest fallback behavior.
  - `_exact_or_latest_usable_state()` or replacement helper.
  - `_apply_cohort_warm_start()` handling of scheduler-prefilled
    `init_state_*` fields.
  - `trigger_forecast()` / `_trigger_forecast()` no-mutation boundary.
- `services/orchestrator/chain_types.py`
  - `InitialStateSelection` evidence fields if needed.
- `packages/common/state_lineage.py`
  - stable rejection/error codes if implementation centralizes them there.
- `infra/env/compute.example`
- Tests:
  - `tests/test_warm_start.py`
  - `tests/test_warm_start_chaining.py`
  - `tests/test_production_scheduler.py`

## Must Preserve

- Non-strict forecast and analysis paths may continue current latest-usable
  fallback/cold-start behavior for existing tests and explicit bootstrap-style
  workflows.
- Existing manifest schema remains backward compatible for non-strict runs.
- Existing runtime state checkpoint emission remains `[6, 12]` where forecast
  horizon supports it.
- Strict failures do not create partial run state, manifests, hydro_run rows, or
  Slurm submissions.
- Existing state lineage/QC rejection evidence remains stable and is not renamed
  without compatibility mapping.

## Must Add / Change

- Add strict mode config:
  - Env: `NHMS_REQUIRE_FORECAST_WARM_START`.
  - Production env example sets it to `true`.
  - Default must preserve non-production compatibility unless implementation can
    update all affected callers safely.
- Strict mode selection:
  - `state_manager is None` blocks.
  - Exact state lookup for `valid_time == cycle_time` is mandatory.
  - Exact miss does not call `get_latest_usable_state()`.
  - Unusable exact state blocks.
  - State-variable QC failure blocks.
  - Source/package checksum/version lineage mismatch blocks.
  - Production business forecast requires `lead_hours == 12`.
- Strict error/evidence:
  - Missing exact successor: stable code such as
    `warm_start_successor_checkpoint_missing`.
  - Unusable/QC-failing exact successor: stable code such as
    `warm_start_successor_checkpoint_unusable`.
  - Lineage/source/package/lead mismatch: stable code such as
    `warm_start_lineage_mismatch`.
  - The exact chosen state flows consistently into run context, run manifest,
    cycle-stage basin entries, and scheduler evidence.
- Scheduler-prefilled state:
  - Prefilled `init_state_uri`/`init_state_id` is not accepted on trust in
    strict mode.
  - The same validator checks valid_time, lead, lineage, usable flag, and QC.
  - Invalid prefilled state blocks before downstream mutation.

## Risk Packs Considered

- Public API / CLI / script entry: selected - orchestrator entrypoints and env
  config behavior change.
- Config / project setup: selected - new production env.
- File IO / path safety / overwrite: selected - no manifest may be written on
  strict failure.
- Schema / columns / units / field names: not selected - no planned DB schema
  change.
- Auth / permissions / secrets: not selected - no credential handling change.
- Concurrency / shared state / ordering: selected - warm-start state must be
  exact predecessor before submit.
- Resource limits / large input / discovery: not selected - no discovery loop
  expansion; strict mode should reduce fallback scanning.
- Legacy compatibility / examples: selected - non-strict and analysis behavior
  must remain available.
- Error handling / rollback / partial outputs: selected - failure must be
  stable and no-mutation.
- Release / packaging / dependency compatibility: not selected - no dependency
  change.
- Documentation / migration notes: selected - production env example changes.
- Geospatial / CRS / basin geometry: not selected - no basin geometry, CRS, or
  vector/raster shape change.
- Hydro-met time series / forcing windows: selected - warm-start time must align
  with business cycle time.
- SHUD numerical runtime / conservation / NaN: selected - cold/fallback state
  would alter numerical continuity.
- PostGIS / TimescaleDB domain behavior: not selected - no query semantics or
  schema change is planned.
- Slurm production lifecycle / mock-vs-real parity: selected - invalid warm
  start must not submit.
- External hydro-met providers / snapshot reproducibility: not selected - no
  provider snapshot or adapter behavior changes; #497 owns adapter defaults.
- Run manifest / QC provenance: selected - selected state evidence must bind to
  manifest/run/cycle.
- Published NHMS artifacts / display identity: not selected - no publish/display
  mutation.

## Invariant Matrix

Governing invariant: In strict forecast warm-start mode, a business forecast run
may be staged or submitted only when its initial state is an exact, usable,
QC-passing, lineage-compatible successor checkpoint for the target cycle.

Source-of-truth identity/contract:
`NHMS_REQUIRE_FORECAST_WARM_START` / `OrchestratorConfig`, target
`source_id`, `model_id`, `cycle_time`, model package version/checksum, and the
selected state snapshot's `valid_time`, `lead_hours`, `source_id`, package
lineage, checksum, and usable/QC status.

Surfaces:

- Producers: state checkpoint save path and scheduler candidate state evidence.
- Validators/preflight: strict config parser, exact successor state lookup,
  lineage/QC validator, scheduler-prefilled state validator.
- Storage/cache/query: state snapshot repository lookups and hydro_run create
  boundaries.
- Public routes/entrypoints: direct `trigger_forecast`,
  `trigger_forecast_from_canonical`, and `orchestrate_cycle`.
- Frontend/downstream consumers: run manifest readers and scheduler evidence.
- Failure paths/rollback/stale state: strict failure before manifest/hydro_run
  write and before Slurm submit.
- Evidence/audit/readiness: manifest `initial_state`, cycle-stage entries,
  rejection codes, and PR validation evidence.

Regression rows:

- Strict `00 -> 12`: exact state with `valid_time == 12Z` and `lead_hours=12`
  is selected and appears consistently in manifest/context/evidence with
  runtime `init_mode=3` and not `quality=cold_start_no_state`.
- Strict `12 -> next-day 00`: exact state with `valid_time == 00Z` and
  `lead_hours=12` is selected with runtime `init_mode=3`.
- Strict exact state missing -> `warm_start_successor_checkpoint_missing`
  before manifest/hydro_run/Slurm mutation.
- Strict exact state present but `usable_flag=false` or QC hook fails ->
  `warm_start_successor_checkpoint_unusable` before mutation.
- Strict exact state has wrong source/package/checksum or `lead_hours != 12` ->
  `warm_start_lineage_mismatch` before mutation.
- Strict `state_manager is None` ->
  `warm_start_successor_checkpoint_missing` before mutation.
- Scheduler-prefilled invalid `init_state_uri`/lineage -> same strict validator
  blocks; prefilled valid exact successor passes.
- Non-strict forecast with no state or older latest usable state -> legacy tests
  remain compatible.
- Analysis warm-start path -> continues latest usable behavior unless explicitly
  placed under strict forecast mode.

## Boundary-Surface Checklist

- Shared helper roots: strict config parser and state validator should have one
  source of truth.
- Public entrypoints: direct forecast and cohort orchestration.
- Read surfaces: state snapshot exact lookup, optional QC hook, scheduler
  candidate state evidence.
- Write/delete/overwrite surfaces: run manifest, hydro_run create/update, Slurm
  submit; all must be skipped on strict failure.
- Staging/publish/rollback surfaces: no publish mutation.
- Producer/consumer evidence boundaries: manifest and cycle-stage entries must
  reference the same selected state.
- Stale-state/idempotency boundaries: repeated strict failure should be stable
  and not create duplicate partial rows.
- Unchanged downstream consumers: non-strict and analysis tests.

## Review Focus

- Strict mode blocks before any irreversible side effect.
- No latest-usable fallback in strict mode.
- Scheduler-prefilled warm-start fields cannot bypass validation.
- `lead_hours == 12` is enforced for strict business forecast, not `f006`.
- Non-strict/analysis compatibility remains intentional and tested.
- Deep SHUD numerical conservation smoke is out of scope for this PR; runtime
  warm-start evidence is the manifest `init_mode=3`, exact state identity, and
  absence of cold-start quality.
