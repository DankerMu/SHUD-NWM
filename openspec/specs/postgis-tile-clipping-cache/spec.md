# postgis-tile-clipping-cache Specification

## Purpose
TBD - created by archiving change m16-production-mvt-performance. Update Purpose after archive.
## Requirements
### Requirement: PostGIS clipping and cache
The tile service SHALL clip/simplify in PostGIS, respect feature/coordinate budgets, and maintain `map.tile_layer`/`map.tile_cache` metadata.

#### Scenario: SQL shape
WHEN building a production tile query
THEN the query uses tile-envelope clipping/simplification and MVT geometry/encoding primitives instead of materializing a full national GeoJSON FeatureCollection in application memory

#### Scenario: Web Mercator tile matrix
WHEN encoding z/x/y MVT
THEN tile bounds use standard Web Mercator XYZ semantics, source geometries are transformed before encoding, extent/buffer/simplification are documented, and encoded coordinates remain inside the allowed MVT extent plus buffer

#### Scenario: Cache hit
WHEN tile has valid cached checksum/etag
THEN service serves or records the cached tile without duplicate cache rows

#### Scenario: Cache identity
WHEN layer, run/source/version, valid_time, z/x/y, style-affecting parameters, or encoder/schema version changes
THEN cache identity changes or the prior cache entry is invalidated before serving

#### Scenario: Oversized tile
WHEN tile exceeds configured feature/coordinate/payload budget
THEN service returns bounded degraded/error response and records evidence

#### Scenario: Real PostGIS opt-in
WHEN real PostGIS credentials are absent
THEN live SQL proof is recorded as not_executed/release_blocked without failing deterministic CI or claiming readiness

### Requirement: Budget-window truncation is an observable runtime event

The shared final SELECT of `postgis_tile_sql` SHALL project `prefilter_stats.intersecting_feature_count`
and `prefilter_stats.intersecting_coordinate_count` (the pre-truncation totals over `bounded_rows`) for
every tile layer, and the single production bind site `_fetch_postgis_tile_bytes` SHALL emit exactly one
`WARNING` log record (logger `apps.api.routes.hydro_display`, token `MVT_TILE_BUDGET_TRUNCATED`, carrying
`layer_id`, `z`, `x`, `y`, `feature_count`, `intersecting_feature_count`, `max_features` (the layer's `feature_limit(layer)`, the same value bound as `:feature_limit`),
`coordinate_count`, `intersecting_coordinate_count`, `max_coordinates`) whenever a tile is generated — a cache
miss on both the database and file tiers; cache hits never re-enter the bind site, so the record counts generations,
not served responses — with
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
THEN no `MVT_TILE_BUDGET_TRUNCATED` record is emitted (those paths emit their own `MVT_TILE_FEATURE_OVERFLOW_BLANKED` record instead)

#### Scenario: Every column the route reads is projected by every layer
WHEN `_fetch_postgis_tile_bytes` reads a column from the tile row
THEN `postgis_tile_sql(layer)` projects a column of that exact name for all five layers, locked by a
source-scan test

### Requirement: The national discharge layer has its own feature budget

`services/tiles/mvt.py::feature_limit(layer)` SHALL return `NATIONAL_DISCHARGE_FEATURE_LIMIT` (20 000) for `hydro-national` and `MVT_MAX_FEATURES` for every other layer and for `None`. `_postgis_tile_params` SHALL bind `:feature_limit` from it, and `_fetch_postgis_tile_bytes` SHALL use the same value for its 413 predicate, the 413 `max_features` detail and the `MVT_TILE_BUDGET_TRUNCATED` `max_features` field. Because this changes `hydro-national` tile bytes wherever more than 10 000 features intersect, `NATIONAL_DISCHARGE_QUERY_VERSION` SHALL be `fair-network-budget-v6`.

#### Scenario: A national discharge tile above the old limit is no longer truncated
- **WHEN** a `hydro-national` tile intersects 13 770 features and no coordinate limit is reached
- **THEN** all 13 770 are selected and no `MVT_TILE_BUDGET_TRUNCATED` record is emitted

#### Scenario: The fair window still truncates above the new limit, observably
- **WHEN** a `hydro-national` tile intersects 21 000 features
- **THEN** 20 000 are selected and one `MVT_TILE_BUDGET_TRUNCATED` record carries `max_features=20000`

#### Scenario: Every other layer keeps the global feature budget
- **WHEN** `_postgis_tile_params` is built for `river-network`, `river-network-national`, `hydro` or `met-stations`
- **THEN** its `feature_limit` equals `MVT_MAX_FEATURES`

### Requirement: A tile blanked by a per-feature overflow is an observable runtime event

When a generated tile row has `feature_coordinate_overflow_count > 0` or `coordinate_dimension_overflow_count > 0` — either of which empties the shared `budget_gate` and so yields a tile with zero features — `_fetch_postgis_tile_bytes` SHALL emit exactly one `WARNING` record on logger `apps.api.routes.hydro_display` with token `MVT_TILE_FEATURE_OVERFLOW_BLANKED`, carrying `layer_id`, `z`, `x`, `y`, `feature_coordinate_overflow_count`, `feature_coordinate_count`, `max_feature_coordinates`, `coordinate_dimension_overflow_count`, `coordinate_dimension_count` and `max_coordinate_dimensions`, the two maxima being the values bound for that query. HTTP status, response bytes, caching and SQL text SHALL be unchanged by this requirement.

#### Scenario: One oversized feature blanks the tile and is reported
- **WHEN** the tile row has `feature_coordinate_overflow_count = 1` and `tile` NULL
- **THEN** the route returns empty bytes with HTTP 200, and emits one `MVT_TILE_FEATURE_OVERFLOW_BLANKED` record and no `MVT_TILE_BUDGET_TRUNCATED` record

#### Scenario: A dimension overflow is reported the same way
- **WHEN** the tile row has `coordinate_dimension_overflow_count = 1` and `tile` NULL
- **THEN** one `MVT_TILE_FEATURE_OVERFLOW_BLANKED` record is emitted

#### Scenario: No overflow, no record
- **WHEN** both overflow counts are 0
- **THEN** no `MVT_TILE_FEATURE_OVERFLOW_BLANKED` record is emitted

