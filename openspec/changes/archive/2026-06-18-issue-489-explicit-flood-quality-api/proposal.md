# Change: Explicit flood quality API contract

## Issue

Implements GitHub issue #489, after #487 introduced explicit
`flood.run_product_quality` fields and #488 stopped writing empty no-curve
`flood.return_period_result` rows.

## Problem

API, forecast-store, and MVT readiness paths still infer flood product quality
from result-row counts. After #488, an all-no-curve run can legitimately have
zero `return_period_result` rows while still having a durable explicit
unavailable quality row. Count inference can misclassify this state as missing
source identity, a misleading 404, or ready when only meaningful partial rows
remain.

## Goals

- Make explicit `flood.run_product_quality` fields the preferred readiness
  contract for flood return-period quality.
- Keep q_down/discharge product readiness independent from unavailable flood
  return-period products.
- Ensure flood-return-period / warning-level metadata, valid-time discovery,
  route gates, and forecast availability expose unavailable/degraded quality
  with stable details.
- Preserve full-curve ready behavior for summary/ranking/timeline/MVT paths.

## Non-Goals

- Do not change worker write behavior; #488 owns that.
- Do not clean historical rows or reclaim storage; #490/#491 own that.
- Do not redesign frontend visuals.
- Do not introduce a new DB migration unless implementation discovers an
  unavoidable contract gap; existing #487 fields are the intended source.

## Risk Triage

Fixture level: expanded.

Repair intensity: broad-expanded.

Risk packs considered:

- Public API / CLI / script entry: selected because REST routes, tile routes,
  and forecast-store response payloads change readiness semantics.
- Config / project setup: not selected; no config or dependency change.
- File IO / path safety / overwrite: not selected; no filesystem behavior.
- Schema / columns / units / field names: selected because existing DB explicit
  quality fields and API/OpenAPI/generated types may need synchronized payload
  fields.
- Auth / permissions / secrets: not selected; no auth boundary change.
- Concurrency / shared state / ordering: selected lightly because routes must
  handle missing/old quality rows during deployment without inconsistent 500s.
- Resource limits / large input / discovery: selected because route queries
  must not reintroduce large `return_period_result` aggregation.
- Legacy compatibility / examples: selected because legacy rows/count fallback
  must remain safe until all environments are migrated.
- Error handling / rollback / partial outputs: selected because unavailable
  flood products must return stable 409/details instead of misleading 404/500.
- Release / packaging / dependency compatibility: not selected unless generated
  frontend types change.
- Documentation / migration notes: selected for PR evidence and OpenAPI/type
  synchronization if response schemas change.
- Geospatial / CRS / basin geometry: not selected; no geometry semantics.
- Hydro-met time series / forcing windows: selected because q_down valid-times
  and display readiness must stay independent from flood quality.
- SHUD numerical runtime / conservation / NaN: not selected.
- PostGIS / TimescaleDB domain behavior: selected because queries target
  flood/hydro tables and must stay index-friendly.
- Slurm production lifecycle / mock-vs-real parity: not selected.
- External hydro-met providers / snapshot reproducibility: not selected.
- Run manifest / QC provenance: not selected.
- Published NHMS artifacts / display identity: selected because layer catalog,
  tile availability, and display products are user-facing.

## Invariant Matrix

Governing invariant: q_down/discharge readiness is independent from flood
return-period readiness, and flood return-period readiness is read from explicit
run quality when available.

Source-of-truth identity/contract: `flood.run_product_quality.run_id` with
`quality_state`, `unavailable_products`, `residual_blockers`, expected counts,
meaningful counts, and no-curve counters.

Surfaces:

- Producers: none in this issue; #487/#488 produce quality rows.
- Validators/preflight: API route gates and MVT source identity checks.
- Storage/cache/query: reads from `flood.run_product_quality`,
  `flood.return_period_result`, and `hydro.river_timeseries`.
- Public routes/entrypoints: `apps/api/routes/flood_alerts.py` layer catalog,
  valid-times, MVT/GeoJSON/tile routes.
- Frontend/downstream consumers: forecast-store latest QHH product,
  `/runs` product quality, OpenAPI/generated frontend types if changed.
- Failure paths/rollback/stale state: missing quality row fails closed for flood
  when the explicit table exists; missing table or missing explicit columns uses
  the documented legacy lightweight fallback without blocking q_down.
- Evidence/audit/readiness: tests must prove all-no-curve, partial-curve, and
  full-curve behaviors are distinct and stable.

Regression rows:

- All-no-curve explicit quality + q_down rows -> layers expose discharge and
  flood metadata as unavailable with blockers and explicit counters:
  `expected_result_rows=2`, `meaningful_result_rows=0`,
  `no_frequency_curve_rows=2`.
- Same run flood tile request -> stable 409 `FLOOD_PRODUCT_UNAVAILABLE` with
  quality details before misleading source-identity 404.
- Partial-curve explicit quality -> not ready, details show
  `expected_result_rows=4`, `meaningful_result_rows=2`,
  `no_frequency_curve_rows=2`.
- Full-curve explicit ready quality -> latest ready flood run and MVT behavior
  remain ready with expected/meaningful counts equal and no no-curve counters.
- Table exists but run has no quality row -> flood quality is unavailable while
  q_down remains available.
- Table or explicit columns missing on a read replica -> legacy lightweight
  result-row existence fallback is used and q_down remains available.

## Boundary Checklist

- Shared helper roots: forecast-store quality parsing helpers.
- Public entrypoints: flood alert routes, MVT route gates, latest product APIs.
- Read surfaces: SQL selects/join fragments over `flood.run_product_quality`.
- Error surfaces: 409 vs 404 ordering for unavailable flood products.
- Unchanged sibling consumers: q_down/discharge valid-time and layer catalog
  behavior.
