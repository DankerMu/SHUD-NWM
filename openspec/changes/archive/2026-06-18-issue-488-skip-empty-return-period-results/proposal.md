# Change: Skip empty flood return-period result rows

## Issue

Implements GitHub issue #488, the next #486 remediation step after #487 made
`flood.run_product_quality` an explicit run-level quality source.

## Problem

`workers/flood_frequency/return_period.py` currently builds and upserts
`flood.return_period_result` rows even when a segment has no usable frequency
curve. Those rows carry `return_period=NULL`, `warning_level=NULL`, and
`quality_flag` values such as `no_frequency_curve` or
`no_usable_frequency_curve`. In no-curve basins this creates large volumes of
empty rows and index growth without producing meaningful flood products.

The current cleanup order also deletes prior peak/timestep rows only when the
new row batch is non-empty, so a recomputation that produces no meaningful rows
can leave stale rows from an earlier run.

## Goals

- Stop writing future empty `return_period_result` rows for no-curve and
  unusable-curve segments.
- Always clear the current run/network's prior return-period rows before
  writing the meaningful replacement rows.
- Preserve explicit `run_product_quality` summaries with expected coverage,
  meaningful rows, no-curve counters, unavailable products, and residual
  blockers even when zero result rows are written.
- Keep q_down/discharge production and tile-layer registration independent from
  missing flood frequency curves.

## Non-Goals

- Do not bulk-clean historical rows for other runs; #490/#491 own cleanup and
  storage reclamation.
- Do not change DB schema, indexes, VACUUM, or REINDEX behavior.
- Do not switch API/MVT/forecast consumers to explicit quality; #489 owns that.
- Do not change hydro `river_timeseries` or q_down extraction semantics.

## Risk Triage

Fixture level: expanded.

Repair intensity: high.

Risk packs considered:

- Public API / CLI / script entry: selected because `compute_return_periods()`
  is the worker entrypoint and its returned stats must remain compatible.
- Config / project setup: not selected; no config, env var, dependency, or
  project setup change.
- File IO / path safety / overwrite: not selected; no filesystem reads/writes.
- Schema / columns / units / field names: selected because existing DB columns
  keep their names but row-count semantics shift from source rows to explicit
  quality summaries.
- Auth / permissions / secrets: not selected; no security boundary changes.
- Concurrency / shared state / ordering: selected because delete-before-write
  ordering must be idempotent for repeated computations of the same run.
- Resource limits / large input / discovery: selected because the main
  production risk is large no-curve basins producing huge empty row batches.
- Legacy compatibility / examples: selected because complete-curve behavior,
  old helper tests, and single-row test helper behavior must remain compatible.
- Error handling / rollback / partial outputs: selected because partial-curve
  runs must keep meaningful rows while representing skipped rows in quality.
- Release / packaging / dependency compatibility: not selected; no package or
  dependency changes.
- Documentation / migration notes: selected for issue/PR evidence only; no user
  docs or migration guide is required because schema and operations are not
  changed.
- Geospatial / CRS / basin geometry: not selected; no geometry, CRS, or vector
  matching logic changes.
- Hydro-met time series / forcing windows: selected lightly because q_down
  peak/timestep coverage counts must remain tied to the existing forecast
  window extraction.
- SHUD numerical runtime / conservation / NaN: not selected; no SHUD simulation
  or numerical solver behavior changes.
- PostGIS / TimescaleDB domain behavior: selected because the worker changes
  writes/deletes against production flood tables.
- Slurm production lifecycle / mock-vs-real parity: not selected; no scheduler
  or gateway behavior changes.
- External hydro-met providers / snapshot reproducibility: not selected; no
  GFS/IFS/ERA5 provider boundary change.
- Run manifest / QC provenance: not selected; no manifest or QC artifact change.
- Published NHMS artifacts / display identity: selected because flood tile
  layers must remain absent for no-curve runs while q_down stays unaffected.

## Invariant Matrix

Governing invariant: unavailable frequency-curve coverage must be represented
in `flood.run_product_quality`, not by empty `flood.return_period_result` rows.

Source-of-truth identity/contract: `(run_id, river_network_version_id)` result
rows for meaningful flood products plus `flood.run_product_quality.run_id`
explicit quality counters and blockers.

Surfaces:

- Producers: `workers/flood_frequency/return_period.py::_compute_return_periods`.
- Validators/preflight: curve availability classification and quality contract
  normalization.
- Storage/cache/query: deletes/upserts in `flood.return_period_result`; explicit
  writes to `flood.run_product_quality`.
- Public routes/entrypoints: `compute_return_periods()` return stats remain
  compatible; route-level readiness switching is #489.
- Frontend/downstream consumers: tile layer registration remains absent when no
  meaningful flood return-period product exists.
- Failure paths/rollback/stale state: recomputation must clear prior rows before
  deciding that there are no rows to write.
- Evidence/audit/readiness: tests must prove no-curve counts/blockers survive
  without source rows.

Regression rows:

- All segments no curve -> zero `return_period_result` rows, explicit
  unavailable quality with expected/no-curve counts and blockers.
- Partial curves -> only usable-curve rows are stored, quality is degraded, and
  expected rows exceed meaningful rows.
- Recompute after stale empty rows -> stale no-curve/null rows for the current
  run/network are deleted even when replacement rows are empty.
- Complete curves -> peak/timestep rows and warning levels remain written.
- Warning thresholds unavailable with usable curves -> rows may remain because
  return periods are meaningful, but quality is unavailable/degraded as in #487.

## Boundary Checklist

- Write/delete surfaces: `_delete_all_prior_peaks`,
  `_delete_all_prior_timesteps`, `_batch_upsert_return_period_results`.
- Stale-state/idempotency: repeated `compute_return_periods()` for the same run.
- Shared quality boundary: `ExplicitFloodRunProductQuality` counters and
  residual blockers.
- Unchanged sibling consumers: q_down extraction, `register_flood_tile_layer`,
  and single-row `_upsert_return_period_result` test helper compatibility.
