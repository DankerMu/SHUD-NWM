# Change: Run-level flood product quality source

## Issue

Implements GitHub issue #487, the root dependency for #486 flood
`return_period_result` growth remediation.

## Problem

`flood.run_product_quality` is currently a count cache derived from
`flood.return_period_result`. That makes null `return_period_result` rows the
only durable way to express no-curve and partial-curve quality. Future issues
must stop writing and then clean those rows, so run-level quality needs to
become an explicit source of truth first.

## Goals

- Extend `flood.run_product_quality` so it can store explicit run quality:
  `quality_state`, unavailable products, residual blockers, expected coverage,
  meaningful result counts, and no-curve reason counts.
- Keep existing count fields and historical backfill compatibility.
- Add helper APIs that can persist explicit unavailable/degraded/ready quality
  without requiring source `return_period_result` rows.
- Avoid creating new NULL partial indexes on `flood.return_period_result`.

## Non-Goals

- Do not change the return-period worker write strategy; #488 owns that.
- Do not switch API/MVT/forecast readiness fully to explicit quality; #489 owns
  that, but this change must provide compatible fields/helpers.
- Do not delete historical rows, drop indexes, vacuum, reindex, or reclaim
  production disk space; #490/#491 own those operations.

## Risk Triage

Fixture level: expanded.

Repair intensity: high.

Selected risk packs:

- PostGIS / TimescaleDB domain behavior: selected because the migration touches
  production flood tables and must avoid hypertable index amplification.
- DB schema / audit contract: selected because quality rows become the source of
  future cleanup and readiness decisions.
- Shared helper behavior: selected because `packages/common/flood_quality.py`
  is used by worker, API, forecast, and future cleanup paths.
- Backward compatibility / historical backfill: selected because existing
  result-row-derived quality must keep working until #488/#489/#490 land.
- Published NHMS artifacts / display identity: selected because q_down readiness
  must not be marked failed by unavailable flood products.

Not selected risk packs:

- Frontend contract: not selected for direct implementation because #489 owns
  API/OpenAPI/frontend surface changes.
- Slurm lifecycle: not selected; no scheduler or gateway behavior changes.
- External provider snapshots: not selected; no GFS/IFS/ERA5 semantics change.
- SHUD runtime / numerical behavior: not selected; no SHUD simulation logic
  changes.

## Invariant Matrix

Governing invariant: run-level flood product quality must remain truthful even
when `return_period_result` has zero rows for unavailable products.

Source-of-truth identity/contract: `flood.run_product_quality.run_id` plus
explicit quality fields (`quality_state`, unavailable products, residual
blockers, expected/meaningful/no-curve counts).

Surfaces:

- Producers: `packages/common/flood_quality.py` explicit write helpers and
  historical backfill helpers.
- Validators/preflight: DB migration tests and helper input validation.
- Storage/cache/query: `flood.run_product_quality` migration and upsert SQL.
- Public routes/entrypoints: read compatibility helpers consumed by API/forecast
  remain stable; full route switching is #489.
- Frontend/downstream consumers: existing count fields remain present.
- Failure paths/rollback/stale state: empty source result rows must not delete
  explicit unavailable rows; stale backfill rows remain cleanable by legacy
  backfill functions.
- Evidence/audit/readiness: tests must prove no-curve and partial-curve states
  cannot be misreported as ready.

Regression rows:

- Explicit all-no-curve quality with zero meaningful rows -> stored row remains
  `quality_state=unavailable` with no-curve counts and blockers.
- Partial-curve quality where expected > meaningful -> stored row is not
  `ready`.
- Historical `return_period_result` source rows -> backfill still produces
  compatible count fields.
- Empty source rows after explicit quality exists -> refresh/backfill does not
  blindly delete the explicit unavailable row.

## Boundary Checklist

- Shared helper roots: `packages/common/flood_quality.py`.
- Public entrypoints: helper functions imported by worker/API/forecast.
- Read surfaces: SQL selects from `flood.run_product_quality`.
- Write surfaces: migration DDL and quality upsert/delete helpers.
- Stale-state/idempotency: repeated writes for the same `run_id`, empty-source
  refresh, and backfill orphan cleanup.
- Unchanged downstream consumers: existing count fields and quality dataclass
  compatibility until #489.
