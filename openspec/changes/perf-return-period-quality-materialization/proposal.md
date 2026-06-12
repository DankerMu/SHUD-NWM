## Why

The current `flood_product_ready` read path scans and aggregates
`flood.return_period_result` per run, causing 12-15s cold directory queries on
node-27 and a worse scaling curve as river segment counts grow. We need the
read path to depend on run count, not on the full return-period result table.

## What Changes

- Add run-level flood product quality materialization for
  `flood.return_period_result` completeness.
- Add partial indexes for null `return_period` and `warning_level` violations so
  quality backfill and fallback probes can find incomplete runs without full
  table scans.
- Rewrite `packages/common/forecast_store.py`, `/api/v1/layers` quality
  annotation, and unscoped flood valid-times latest-ready selection to use the
  run-level quality table instead of per-request readiness aggregation over
  `flood.return_period_result`.
- Add a maintainer/operator backfill path for existing runs and worker-side
  refresh when frequency products complete.
- Preserve existing product-quality response fields and readiness semantics.
- Record node-22/node-27 live receipts when environment access is available, but
  do not make node-27 latency timing a merge gate for this PR per the
  2026-06-13 scope update; never fake live receipts.

## Capabilities

### New Capabilities

- `return-period-run-quality-materialization`: Run-level materialized flood
  return-period quality used by display and API read paths.

### Modified Capabilities

<!-- No existing archived spec capability is modified. This change introduces a
new performance/read-path capability and keeps public API semantics stable. -->

## Impact

- Database migrations under `db/migrations/`.
- `packages/common/forecast_store.py` run quality SQL and readiness filtering.
- `apps/api/routes/flood_alerts.py` layer quality metadata.
- `services/tiles/mvt.py` latest-ready run selection for flood valid-times.
- Worker or helper code that refreshes quality after flood frequency completion.
- Backfill/operator script for existing `flood.return_period_result` rows.
- Tests for migrations, forecast API SQL shape/readiness semantics, and backfill
  behavior.
- Local deterministic SQL-shape, migration, refresh/backfill, route, and review
  evidence; node-22/node-27 live receipts remain environment-owned follow-up
  evidence when unavailable locally.
