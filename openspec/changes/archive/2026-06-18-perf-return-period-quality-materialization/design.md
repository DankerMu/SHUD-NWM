## Context

Issue #455 targets the cold read path for display directory endpoints:
`/api/v1/runs?flood_product_ready=true`, `/api/v1/layers`, and
`/api/v1/layers/flood-return-period/valid-times`. The current
`packages/common/forecast_store.py` implementation computes flood product
quality through a `LEFT JOIN LATERAL` aggregate over
`flood.return_period_result` for each candidate run. On the production-sized
node-27 readonly replica this is already a 12-15s cold path, and it scales with
return-period result rows rather than with runs.

Fixture level: expanded.
Repair intensity: broad-expanded, because the change touches DB schema,
production read-path SQL, worker completion behavior, backfill/maintenance
scripts, and live evidence gates.
Project profile: NHMS.

## Goals / Non-Goals

**Goals:**

- Make `flood_product_ready` read paths use run-level quality data or bounded
  violation probes rather than full `return_period_result` aggregation.
- Preserve existing API response fields: `flood_quality_max_over_window`,
  `flood_result_rows`, `flood_return_period_rows`, `flood_warning_rows`, and
  derived product-quality semantics.
- Add a reproducible backfill/refresh path for existing and newly completed
  runs.
- Keep node-22 migration/backfill evidence and node-27 cold-query receipt as
  explicit operator follow-up evidence when live access is available.

**Non-Goals:**

- No frontend behavior changes and no cache semantics changes from PR #453.
- No change to public API schemas beyond preserving existing quality fields.
- No reinterpretation of readiness: ready still requires positive result rows,
  positive return-period rows, complete return-period coverage, and warning
  coverage for return-period rows.
- No fake live receipt; when node-22/node-27 access is unavailable from the
  development machine, record that status rather than treating latency timing
  as a merge-blocking acceptance criterion.

## Decisions

### D1. Store run-level quality in `flood.run_product_quality`

Create a small table keyed by `run_id` with the exact row counts required to
reconstruct existing quality fields. The materialized row must support the
current consumers without silently normalizing their pre-change differences:

- Common stored counts: total rows, peak rows, non-null return-period rows for
  all rows, non-null warning rows for all rows, non-null return-period rows for
  peak rows, non-null warning rows for peak rows, and `refreshed_at`.
- `ForecastStore` compatibility: `quality_max_over_window` and `result_rows`
  keep the current peak-if-present fallback; `return_period_rows` keeps the
  current peak-only count; `warning_rows` keeps the current peak-if-present,
  all-row fallback.
- `apps/api/routes/flood_alerts.py::_flood_product_quality` compatibility:
  first evaluate peak rows (`max_over_window = true`); if no peak result rows
  exist, evaluate all rows and return `max_over_window = None`, matching the
  current layer catalog and readiness error details.
- `services/tiles/mvt.py::latest_ready_run` compatibility: preserve the current
  latest-ready selection formula that uses peak counts when peak rows exist and
  all-row counts otherwise.

Read paths join this table rather than aggregating
`flood.return_period_result` per run.

Alternative considered: only add partial indexes and keep `NOT EXISTS` probes.
That improves violation detection but still cannot expose existing row-count
fields without scanning or maintaining counts.

### D2. Add partial violation indexes

Add partial indexes on `(run_id)` for rows where `return_period IS NULL` and
where `warning_level IS NULL`. These indexes support backfill validation and
fallback probes without scanning healthy result rows.

### D3. Refresh quality at frequency completion and via backfill

Frequency worker completion or an equivalent shared helper updates
`flood.run_product_quality` for the completed run. Existing runs are populated
by a maintainer/operator backfill script that is safe to rerun.

### D4. Keep API semantics stable

`ForecastStore.list_runs` and `get_run`, `/api/v1/layers` flood metadata, and
unscoped flood valid-times run selection keep the same response shape and
availability semantics. If quality materialization is absent for a run,
readiness is unavailable rather than silently claimed ready.

## Risk Packs Considered

- Public API / CLI / script entry: selected - run listing APIs and backfill
  script are public/operator entrypoints.
- Config / project setup: selected - migration/backfill must be deployable on
  node-22 and safe for node-27 replication.
- File IO / path safety / overwrite: not selected - no new file writes beyond
  normal evidence produced by commands.
- Schema / columns / units / field names: selected - new DB table/indexes and
  existing API fields must stay stable.
- Auth / permissions / secrets: not selected - no credential handling changes.
- Concurrency / shared state / ordering: selected - quality refresh must not
  race into stale or partial readiness.
- Resource limits / large input / discovery: selected - the core goal is to
  remove full-table scans from read paths.
- Legacy compatibility / examples: selected - existing quality semantics and
  tests remain compatible.
- Error handling / rollback / partial outputs: selected - missing or partial
  quality must fail closed.
- Release / packaging / dependency compatibility: selected - migration and
  tests must work in existing deployment tooling.
- Documentation / migration notes: selected - node-22/node-27 evidence and
  backfill procedure must be recorded.
- Geospatial / CRS / basin geometry: not selected - no geometry, CRS, basin
  boundary, or vector/raster transform changes.
- Hydro-met time series / forcing windows: not selected - no forcing window or
  hydro timeseries generation changes; valid-times queries keep existing
  identity and duration filtering.
- SHUD numerical runtime / conservation / NaN: not selected - no solver runtime
  or numerical output generation changes.
- PostGIS / TimescaleDB domain behavior: selected - migration touches the flood
  Timescale/Postgres domain.
- Slurm production lifecycle / mock-vs-real parity: not selected - no Slurm
  submission/poll/cancel/status behavior changes.
- External hydro-met providers / snapshot reproducibility: not selected - no
  provider snapshot or ingestion boundary changes.
- Run manifest / QC provenance: not selected - no run manifest or QC schema
  changes; quality provenance is DB-bound by `run_id` and covered by schema and
  display identity packs.
- Published NHMS artifacts / display identity: selected - display readiness
  must remain bound to the same run identity.

## Invariant Matrix

Governing invariant: flood product readiness for a run is derived from quality
data bound to exactly that `run_id`, and display read paths must not aggregate
the full `flood.return_period_result` table per request.

Source-of-truth identity/contract: `hydro.hydro_run.run_id` and
`flood.run_product_quality.run_id`.

Surfaces:

- Producers: `workers/flood_frequency/return_period.py` or shared quality
  refresh helper.
- Validators/preflight: backfill script and migration tests.
- Storage/cache/query: `flood.run_product_quality`, partial violation indexes,
  `packages/common/forecast_store.py`, `apps/api/routes/flood_alerts.py`,
  `services/tiles/mvt.py`.
- Public routes/entrypoints: `/api/v1/runs`, `/api/v1/layers`,
  `/api/v1/layers/{layer_id}/valid-times`.
- Frontend/downstream consumers: existing display consumers and PR #453 cache,
  unchanged.
- Failure paths/rollback/stale state: missing quality row, partial quality row,
  null return-period/warning rows, rerun backfill.
- Evidence/audit/readiness: deterministic local tests, SQL-shape checks,
  OpenSpec validation, and optional node-22/node-27 live receipts when
  environment access is available.

Regression rows:

- Ready run with complete materialized quality -> `flood_product_ready=true`
  includes the run and existing quality fields report `ready`.
- Run with null return periods or warnings -> readiness is unavailable and the
  run is excluded by `flood_product_ready=true`.
- Missing materialized quality row -> readiness is not claimed.
- `list_runs`/`get_run` SQL shape -> does not contain a lateral aggregate over
  `flood.return_period_result`.
- `/api/v1/layers` flood product quality annotation -> uses
  `flood.run_product_quality` and does not run `COUNT/SUM` over
  `flood.return_period_result`.
- Unscoped `/api/v1/layers/flood-return-period/valid-times` and
  `/warning-level/valid-times` latest-ready selection -> does not `GROUP BY`
  or aggregate the full `flood.return_period_result` table.
- Existing display/cache consumers -> response fields and meanings are stable.

## Risks / Trade-offs

- Risk: materialized quality becomes stale after reruns. Mitigation: refresh on
  frequency completion and make backfill idempotent.
- Risk: migration locks a large hypertable. Mitigation: use concurrent or
  low-lock index creation where supported; avoid table rewrites in hot paths.
- Risk: tests pass with deterministic fixtures but live DB remains slow.
  Mitigation: preserve SQL-shape gates in this PR and record node-22/node-27
  receipts as environment-owned follow-up evidence when access is available.
- Risk: missing quality rows hide available products. Mitigation: fail closed
  and make backfill part of deployment acceptance.

## Migration Plan

1. Add DB migration for `flood.run_product_quality` and partial violation
   indexes.
2. Add backfill/refresh helper with deterministic tests.
3. Update `ForecastStore`, layer catalog quality annotation, and unscoped flood
   valid-times latest-ready selection to use the quality table.
4. Run local tests and OpenSpec validation.
5. Capture local deterministic evidence: migration shape, backfill/refresh
   behavior, SQL shape, route semantics, and OpenSpec validation.
6. Live node-22/node-27 receipts are useful operational follow-up evidence but
   are not merge-blocking for this PR per the 2026-06-13 scope update because
   node-27 access is not convenient from this machine.

## Open Questions

- The exact live DB credential and execution path for node-22/node-27 evidence
  are environment-owned. Per the 2026-06-13 scope update, implementation can
  merge after local issue tasks, review, and CI pass; live latency receipts can
  be collected later by operators.
