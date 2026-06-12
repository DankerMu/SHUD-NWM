## 1. Fixture And Risk Gate

- [x] 1.1 Review this OpenSpec fixture with `codeagent-wrapper --backend codex`
  before implementation.
- [x] 1.2 Run `openspec validate perf-return-period-quality-materialization --strict --no-interactive`.
- [x] 1.3 Keep fixture level `expanded` and repair intensity `broad-expanded`
  unless the implementation scope is explicitly narrowed in the issue.

## 2. Database Migration

- [x] 2.1 Add a migration after `000033` that creates
  `flood.run_product_quality` keyed by `run_id`.
- [x] 2.2 Add partial violation indexes for `return_period IS NULL` and
  `warning_level IS NULL` on `flood.return_period_result`.
- [x] 2.3 Keep migration low-lock for production: use concurrent indexes where
  PostgreSQL/Timescale permits and do not rewrite the large result table.
- [x] 2.4 Add migration tests for table, key, columns, and index predicates.

## 3. Quality Refresh And Backfill

- [x] 3.1 Add a shared helper or script that computes quality for one or more
  runs and upserts `flood.run_product_quality` with enough counts to preserve
  `ForecastStore`, `_flood_product_quality`, and `latest_ready_run` semantics.
- [x] 3.2 Call the refresh path from flood frequency completion or document the
  exact worker-side completion hook used.
- [x] 3.3 Add an idempotent maintainer/operator backfill command for existing
  runs.
- [x] 3.4 Test complete rows, null return-period rows, null warning-level rows,
  max-over-window coverage, no-peak fallback for each affected surface,
  `result_rows > return_period_rows`, `warning_rows < return_period_rows`,
  missing quality rows, and repeated backfill.
- [x] 3.5 Test rerun and failure boundaries: refreshed quality overwrites stale
  rows for the same `run_id`; failed/rolled-back frequency processing does not
  leave a ready `flood.run_product_quality` row; partial quality rows fail
  closed.

## 4. Forecast Store Read Path

- [x] 4.1 Rewrite `_flood_product_quality_join` or its replacement so
  `list_runs` and `get_run` use `flood.run_product_quality` instead of lateral
  aggregation over `flood.return_period_result`.
- [x] 4.2 Preserve existing quality response fields and unavailable-product
  derivation.
- [x] 4.3 Update SQL-shape tests so `flood_product_ready=true` no longer emits
  the per-run lateral aggregate.
- [x] 4.4 Verify missing quality rows fail closed and do not claim readiness.

## 4A. Layer Catalog And Valid-Times Read Paths

- [x] 4A.1 Rewrite `apps/api/routes/flood_alerts.py::_flood_product_quality`
  and `_flood_product_quality_counts` or their replacements so `/api/v1/layers`
  uses `flood.run_product_quality` for flood metadata and no longer runs
  `COUNT/SUM` aggregation over `flood.return_period_result`.
- [x] 4A.2 Rewrite `services/tiles/mvt.py::latest_ready_run` so unscoped
  `/api/v1/layers/flood-return-period/valid-times` and
  `/api/v1/layers/warning-level/valid-times` select the latest ready run from
  materialized quality, not a `GROUP BY flood.return_period_result` aggregate.
- [x] 4A.3 Keep actual tile/data retrieval queries scoped to the selected
  `run_id`/identity; this change only removes readiness-quality aggregation
  from directory and discovery paths.
- [x] 4A.4 Add SQL-shape or route tests proving `/api/v1/layers`,
  `latest_ready_run`, and flood valid-times readiness checks do not perform
  full-table readiness aggregation.

## 5. Verification

- [x] 5.1 Run `uv run --no-sync pytest -q tests/test_migrations.py tests/test_forecast_api.py tests/test_flood_alerts_api.py tests/test_return_period.py`.
- [x] 5.2 Run `uv run --no-sync ruff check packages/common apps/api services workers tests/test_migrations.py tests/test_forecast_api.py tests/test_flood_alerts_api.py tests/test_return_period.py`.
- [x] 5.3 Run `openspec validate perf-return-period-quality-materialization --strict --no-interactive`.
- [x] 5.4 Capture node-22 migration/backfill receipt, quality row consistency,
  and EXPLAIN ANALYZE or equivalent optimized query-plan proof if live node-22
  access is available; no local live access was used, so this remains
  follow-up operational evidence and not a merge gate for this PR.
- [x] 5.5 Capture node-27 force-refresh cold-query receipt for `/api/v1/runs`,
  `/api/v1/layers`, and `flood-return-period` valid-times; cache hits alone do
  not satisfy this evidence. Per the 2026-06-13 scope update, node-27 latency is
  not an acceptance criterion for this PR when local access is unavailable.
- [ ] 5.6 Continue review-fix loops until the latest comprehensive review and
  final review report no P0/P1 findings.
- [x] 5.7 Record the backfill procedure and live-receipt follow-up status in PR
  evidence/work summary so operators know what remains environment-owned.

## 6. Non-Goals

- [x] 6.1 Do not change frontend behavior or PR #453 display cache semantics.
- [x] 6.2 Do not claim live node-22/node-27 latency evidence unless receipts are
  actually collected.
