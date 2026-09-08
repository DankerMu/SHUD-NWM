## ADDED Requirements

### Requirement: Budget-window truncation is an observable runtime event

The shared final SELECT of `postgis_tile_sql` SHALL project `prefilter_stats.intersecting_feature_count`
and `prefilter_stats.intersecting_coordinate_count` (the pre-truncation totals over `bounded_rows`) for
every tile layer, and the single production bind site `_fetch_postgis_tile_bytes` SHALL emit exactly one
`WARNING` log record (logger `apps.api.routes.hydro_display`, token `MVT_TILE_BUDGET_TRUNCATED`, carrying
`layer_id`, `z`, `x`, `y`, `feature_count`, `intersecting_feature_count`, `max_features`,
`coordinate_count`, `intersecting_coordinate_count`, `max_coordinates`) whenever a tile is returned with
`(intersecting_coordinate_count > coordinate_count OR intersecting_feature_count > feature_count) AND
feature_coordinate_overflow_count = 0 AND coordinate_dimension_overflow_count = 0`. Tile bytes, HTTP
status semantics (200/413/424/500), binds, cache identity and `*_QUERY_VERSION` literals SHALL be unchanged
by this requirement.

#### Scenario: Fair budget window drops rows
WHEN a window layer (`hydro-national`, `river-network-national`) generates a tile whose intersecting
coordinate total exceeds the bound `:collection_coordinate_limit` and no single feature overflows the
per-feature coordinate or dimension limit
THEN the route returns the truncated tile bytes with HTTP 200 AND one `MVT_TILE_BUDGET_TRUNCATED` WARNING
record with the selected and intersecting counts for that `layer_id/z/x/y`

#### Scenario: Untruncated tile is silent
WHEN the selected feature and coordinate counts equal the intersecting counts
THEN no `MVT_TILE_BUDGET_TRUNCATED` record is emitted

#### Scenario: Non-budget drop paths do not trigger the signal
WHEN `feature_coordinate_overflow_count > 0` or `coordinate_dimension_overflow_count > 0` for the tile,
even though the intersecting counts exceed the selected counts
THEN no `MVT_TILE_BUDGET_TRUNCATED` record is emitted (those paths keep their existing behavior)

#### Scenario: Every column the route reads is projected by every layer
WHEN `_fetch_postgis_tile_bytes` reads a column from the tile row
THEN `postgis_tile_sql(layer)` projects a column of that exact name for all five layers, locked by a
source-scan test
