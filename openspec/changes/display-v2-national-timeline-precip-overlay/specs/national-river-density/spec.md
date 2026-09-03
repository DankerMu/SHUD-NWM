## ADDED Requirements

### Requirement: National river-network tiles use the denser stream_type threshold table
The national river-network MVT SQL SHALL filter `core.river_segment.stream_type` with the threshold table z≤4 → ≥4, z5 → ≥3, z6 → ≥2, z7 → ≥1, z≥8 → ≥1 (one class denser than the previous z≤4 → 5 / z5 → 4 / z6 → 3 / z7 → 2 table), and `NATIONAL_RIVER_NETWORK_QUERY_VERSION` SHALL be bumped to `stream-type-aggregate-v3` so cached tiles are regenerated. The threshold lives in ONE `CASE` expression inside the single SQL string that `postgis_tile_sql(layer: str) -> str` returns for `"river-network-national"`; zoom is a SQL bind (`:z`), not a Python argument, so the assertion seam is substring matching on that one string. The identically shaped `CASE` inside the `hydro-national` SQL is NOT changed by this requirement. The `CASE` is today built once in a `source_cte` shared by BOTH `"river-network"` (per-basin, per-segment, no aggregation) and `"river-network-national"` (the `if layer in {"river-network", "river-network-national"}` branch), so the v3 literals MUST be applied only when `layer == "river-network-national"`; the per-basin `river-network` layer SHALL keep the v2 table unchanged, because at z7 it would otherwise emit the full basin network per tile (a dense basin already packs >50k coordinates into one z7 tile) and nothing in the go/no-go measurement covers that layer.

#### Scenario: Threshold table is encoded in the river-network SQL string
- **WHEN** `postgis_tile_sql("river-network-national")` is generated
- **THEN** the returned SQL contains `WHEN :z <= 4 THEN 4.0`, `WHEN :z = 5 THEN 3.0`, `WHEN :z = 6 THEN 2.0` and `WHEN :z = 7 THEN 1.0`
- **AND** it does NOT contain the v2 bounds `WHEN :z <= 4 THEN 5.0` or `WHEN :z = 7 THEN 2.0`
- **AND** the `hydro-national` SQL still contains its own unchanged `WHEN :z <= 4 THEN 5.0` bound

#### Scenario: Per-basin river-network SQL keeps the v2 table
- **WHEN** `postgis_tile_sql("river-network")` is generated after this change
- **THEN** the returned SQL still contains the v2 bounds `WHEN :z <= 4 THEN 5.0`, `WHEN :z = 5 THEN 4.0`, `WHEN :z = 6 THEN 3.0` and `WHEN :z = 7 THEN 2.0`
- **AND** it does NOT contain `WHEN :z <= 4 THEN 4.0` or `WHEN :z = 7 THEN 1.0`
- **AND** the per-basin `/api/v1/tiles/river-network/{basin_version_id}/{z}/{x}/{y}.pbf` cache generation is unchanged (`river-network` does not consume `NATIONAL_RIVER_NETWORK_QUERY_VERSION`)

#### Scenario: Cache generation changes
- **WHEN** the national river-network cache key is computed after this change
- **THEN** `national_river_network_source_version(session)` starts with `river-network-national:stream-type-aggregate-v3:` (the query version is carried in the source-version string, not embedded in the SQL text) and differs from the v2 key for the same tile

### Requirement: Denser national river tiles stay within the coordinate budget
Before the threshold change is enabled in production, node-27 SHALL measure the coordinate budget of every z3, z4, z6 and z7 tile intersecting the China bounds with the new SQL. The binding metric is `prefilter_stats.feature_coordinate_count` — the MAXIMUM single-feature coordinate count — together with `prefilter_stats.feature_coordinate_overflow_count`, because `budget_stats.coordinate_count` sums only the features that already passed the per-feature limit and therefore reads low (or zero) exactly when a merged trunk feature blew the limit and was filtered out into a silently empty tile. `budget_stats.coordinate_count` MUST also be recorded, as a secondary item. Every measured tile MUST satisfy `feature_coordinate_count < MVT_MAX_COORDINATES` (50 000) AND `feature_coordinate_overflow_count == 0`. If any tile fails either condition, the affected zoom level MUST fall back one class (e.g. z≤4 back to ≥5) and the receipt MUST record the measured values and the fallback. These columns are outputs of the tile SQL (`services/tiles/mvt.py`, final `SELECT`) and are read by running that SQL directly against node-27's database, not from the tile HTTP route, which does not expose them.

#### Scenario: Go decision
- **WHEN** every z3, z4, z6 and z7 China tile measures `feature_coordinate_count < 50 000` and `feature_coordinate_overflow_count == 0`
- **THEN** the v3 threshold table ships unchanged
- **AND** the receipt lists, per tile and per zoom, `feature_coordinate_count`, `feature_coordinate_overflow_count` and `coordinate_count`, before and after the change

#### Scenario: No-go decision
- **WHEN** any measured tile has a single feature at or above 50 000 coordinates, or reports `feature_coordinate_overflow_count > 0`
- **THEN** that zoom level's threshold is raised by one class in the shipped table
- **AND** the receipt and the spec comment record which level was rolled back and why

#### Scenario: Overflow is not masked by the aggregate
- **WHEN** a tile reports `feature_coordinate_overflow_count > 0` while `budget_stats.coordinate_count` is small or zero because the oversized feature was filtered out
- **THEN** the decision is still No-go for that zoom level
- **AND** the receipt records that the aggregate was misleading for that tile

### Requirement: Frontend national river paint is not dimmed at low zoom
`m11NationalRiverPaint` SHALL apply the `dimmed` opacity discount only at zoom ≥ 6 via a zoom-interpolated expression, SHALL use wider z3–z5 line-width stops for trunk classes, and SHALL give the newly visible classes a non-zero opacity where the denser SQL now returns them: the v3 table returns `Type ≥ 2` at z6 and `Type ≥ 1` at z7, while today's `line-opacity` stops list only `Type 5..2` at z7 (`Type 1` falls to the `match` default `0`) and have no z6 stop at all (so `Type 2` interpolates to ≈0.25 and `Type 1` to 0 at z6). Those features would be fetched and drawn invisibly. The paint MUST therefore render `Type 2` at z6 with opacity ≥ 0.4 and `Type 1` with a non-zero opacity at z6 and ≥ 0.3 at z7.

#### Scenario: Low zoom ignores dimming
- **WHEN** `m11NationalRiverPaint({ dimmed: true, satellite: false })` is evaluated
- **THEN** the `line-opacity` expression yields the undimmed value at zoom 3–5.99 and the 0.42-scaled value at zoom ≥ 6

#### Scenario: Newly visible classes are actually visible
- **WHEN** the `line-opacity` expression of `m11NationalRiverPaint({ dimmed: false, satellite: false })` is evaluated at zoom 6 and zoom 7
- **THEN** `Type = 2` at zoom 6 is ≥ 0.4 (it is ≈0.25 today) and `Type = 1` at zoom 6 is > 0 (it is 0 today)
- **AND** `Type = 1` at zoom 7 is ≥ 0.3 (it is 0 today)

#### Scenario: Trunk width stops
- **WHEN** the `line-width` expression is evaluated at zoom 3 and zoom 5
- **THEN** `Type = 4` at zoom 3 is ≥ 1.4 px (it is ≈1.24 px today, so an unchanged paint fails this assertion)
- **AND** `Type = 5` is > 1.5 px at zoom 3 and > 2.3 px at zoom 5 (it is exactly 1.5 px and ≈2.23 px today), so the stops must strictly increase rather than merely satisfy the old values
