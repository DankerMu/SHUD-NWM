## ADDED Requirements

### Requirement: Run-level flood quality is materialized

The system SHALL maintain run-level flood return-period quality rows keyed by
`run_id` so display read paths can evaluate readiness without per-request full
aggregation of `flood.return_period_result`.

#### Scenario: quality is refreshed for a completed run
- **WHEN** flood frequency processing completes for a hydro run
- **THEN** `flood.run_product_quality` contains enough per-run counts to
  reconstruct the current `ForecastStore`, `_flood_product_quality`, and
  `latest_ready_run` formulas without aggregating source result rows at read
  time

#### Scenario: existing runs are backfilled
- **WHEN** the maintainer/operator runs the backfill helper for existing
  return-period results
- **THEN** quality rows are inserted or updated idempotently and are bound to
  the same `run_id` as the source result rows

#### Scenario: rerun replaces stale quality
- **WHEN** a run is recomputed or backfilled after its return-period rows change
- **THEN** the materialized quality row for that `run_id` is overwritten from
  the current source rows and stale readiness is not preserved

#### Scenario: failed processing does not publish ready quality
- **WHEN** flood frequency processing fails or rolls back before the result rows
  are complete
- **THEN** the system does not leave a ready materialized quality row for that
  `run_id`

### Requirement: Display read paths avoid full result aggregation

Display read paths SHALL use run-level quality rows or bounded indexed
violation probes instead of per-request readiness aggregation over
`flood.return_period_result`.

#### Scenario: run list filters ready flood products
- **WHEN** `list_runs(..., flood_product_ready=True)` builds SQL
- **THEN** the SQL joins or probes run-level quality and does not perform a
  per-run aggregate scan of `flood.return_period_result`

#### Scenario: ForecastStore compatibility is preserved
- **WHEN** run quality is rendered through `ForecastStore`
- **THEN** `quality_max_over_window`, `result_rows`, `return_period_rows`, and
  `warning_rows` match the pre-change `ForecastStore` formula, including its
  peak-only `return_period_rows` behavior and warning-row fallback

#### Scenario: run detail reports quality
- **WHEN** `get_run(...)` builds SQL for a hydro run
- **THEN** the SQL uses materialized run-level quality to populate existing
  flood quality fields and does not perform a per-run aggregate scan of
  `flood.return_period_result`

#### Scenario: layer catalog annotates flood quality
- **WHEN** `/api/v1/layers` annotates `flood-return-period` and `warning-level`
  metadata
- **THEN** it uses `flood.run_product_quality` for the selected run instead of
  `COUNT/SUM` aggregation over `flood.return_period_result`

#### Scenario: layer quality compatibility is preserved
- **WHEN** layer catalog readiness is rendered through
  `_flood_product_quality`
- **THEN** it first uses peak rows and falls back to all rows with
  `max_over_window = None` only when no peak result rows exist, matching the
  pre-change API details

#### Scenario: flood valid-times selects latest ready run
- **WHEN** `/api/v1/layers/flood-return-period/valid-times` or
  `/api/v1/layers/warning-level/valid-times` is requested without an explicit
  `run_id`
- **THEN** latest-ready run selection uses materialized quality and does not
  `GROUP BY` or aggregate the full `flood.return_period_result` table

#### Scenario: latest-ready fallback is preserved
- **WHEN** latest-ready run selection evaluates a run with no peak rows but
  complete all-row return-period and warning coverage
- **THEN** it preserves the current all-row fallback behavior

#### Scenario: run quality is missing
- **WHEN** a run has no materialized quality row
- **THEN** the read path does not claim the run is flood-product-ready

### Requirement: Readiness semantics remain stable

The system SHALL preserve existing flood product readiness and product-quality
response semantics while changing the storage/query implementation.

#### Scenario: complete run is listed
- **WHEN** a run has positive result rows, positive return-period rows, no
  return-period nulls, and no warning-level nulls for return-period rows
- **THEN** existing API quality fields report the run as ready and
  `flood_product_ready=true` includes the run

#### Scenario: incomplete run is listed
- **WHEN** a run has result rows but null return-period or warning-level values
- **THEN** existing API quality fields report the same unavailable products and
  `flood_product_ready=true` excludes the run

#### Scenario: no peak rows fallback remains stable
- **WHEN** a run has return-period result rows but no `max_over_window = true`
  rows
- **THEN** readiness and quality fields use all result rows, matching the
  existing fallback behavior

#### Scenario: degraded frequency curves remain degraded
- **WHEN** `result_rows > return_period_rows` for the selected quality window
- **THEN** `frequency_curves` remains unavailable/degraded and the run is not
  treated as fully ready

#### Scenario: warning coverage remains required
- **WHEN** `warning_rows < return_period_rows` for the selected quality window
- **THEN** `warning_thresholds` remains unavailable and the run is excluded by
  ready-only filters

### Requirement: Production evidence is recorded without blocking local closure

The change SHALL record node-22 primary DB migration/backfill evidence and
node-27 readonly force-refresh cold-query evidence when environment access is
available, but local completion is gated by deterministic issue tasks, review,
and CI rather than node-27 latency timing.

#### Scenario: node-22 evidence is collected
- **WHEN** the migration and backfill run on node-22
- **THEN** evidence includes migration receipt, quality row consistency, and
  EXPLAIN ANALYZE or equivalent query-plan proof for the optimized read path

#### Scenario: node-27 display receipt is collected
- **WHEN** node-27 display endpoints are force-refreshed against the readonly
  replica
- **THEN** evidence records cold-query latency for `/api/v1/runs`,
  `/api/v1/layers`, and relevant valid-times endpoints, without relying on
  cache hits as the sole proof

#### Scenario: live latency receipt is unavailable
- **WHEN** node-27 access is unavailable from the development machine
- **THEN** live latency timing is recorded as environment-owned follow-up
  evidence and does not block merge if local issue tasks, review, and CI pass
