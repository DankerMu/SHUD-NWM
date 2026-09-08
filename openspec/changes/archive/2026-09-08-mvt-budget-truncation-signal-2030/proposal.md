# Proposal — mvt-budget-truncation-signal-2030

## Why

The two MVT layers with a fair collection-budget window (`hydro-national`, introduced at `76d38cf1`;
`river-network-national`, introduced by #2005 / PR #2025) drop rows **silently** once a tile crosses
`:feature_limit` / `:collection_coordinate_limit`: `eligible` keeps only the ranked prefix, `budget_stats`
is computed *from* `eligible`, so the route's 413 predicate (`feature_count > MVT_MAX_FEATURES or
coordinate_count > max_coordinates`, `apps/api/routes/hydro_display.py:756`) can never be true on a window
layer — the tile is a 200 with fewer rows and no log line. `prefilter_stats` already computes the
pre-truncation `intersecting_feature_count` / `intersecting_coordinate_count` (`services/tiles/mvt.py`,
`prefilter_stats` CTE) but the shared final SELECT does not project them, so the route physically cannot
see the gap. On node-27 z0/0/0 sits at 114 377 of 120 000 coordinates and every newly activated dense
network moves it up; truncation will appear in production before anyone can notice. Issue #2030.

## What Changes

- `postgis_tile_sql(layer)`: the shared final SELECT (all five layers) additionally projects
  `intersecting_feature_count` and `intersecting_coordinate_count` from `prefilter_stats`. Exactly two
  added lines; predicate chains, `tile` bytes, binds and CTEs unchanged.
- `_fetch_postgis_tile_bytes`: reads the two new columns plus the already-projected
  `feature_coordinate_overflow_count` / `coordinate_dimension_overflow_count`, and after the existing
  500/413/424 raises emits ONE `WARNING` log record (`apps.api.routes.hydro_display`, token
  `MVT_TILE_BUDGET_TRUNCATED`) when `(intersecting_coordinate_count > coordinate_count OR
  intersecting_feature_count > feature_count) AND feature_coordinate_overflow_count = 0 AND
  coordinate_dimension_overflow_count = 0`. Record carries
  `layer_id`, `z/x/y`, both sides' numbers, `max_features`, `max_coordinates`.
- Tests: caplog matrix on the route, five-layer projection assertion, route↔SQL column-coverage lock,
  stub-row helper gains the four columns.
- Evidence: five-layer SQL regression (old-vs-new text diff is exactly the two lines; new sha256[:16]
  digests replace the #2025 receipt table), node-27 live receipt (forced-limit signal fires; production
  bind silent on all 516 China tiles; bytes unchanged on a sample), no `*_QUERY_VERSION` bump.

## Capabilities

### New Capabilities
- (none)

### Modified Capabilities
- `postgis-tile-clipping-cache`: ADDED requirement — budget-window truncation is an observable runtime
  event (the "records evidence" half of the existing *Oversized tile* scenario).

## Impact

- `services/tiles/mvt.py` (`postgis_tile_sql` final SELECT only)
- `apps/api/routes/hydro_display.py` (`_fetch_postgis_tile_bytes`; module-level logger)
- `tests/test_hydro_display_mvt_scaling.py` (new tests; `_budget_row` widened)
- `docs/runbooks/receipts/2026-09-08-issue-2030-budget-truncation-signal-node27.md` (new)
- Not touched: budget values, window ordering, 413 semantics, frontend, cache key / `*_QUERY_VERSION`,
  `tests/fixtures/river_ts_templates_*.json` golden (`sql_chains` is blind to the SELECT list by construction).
