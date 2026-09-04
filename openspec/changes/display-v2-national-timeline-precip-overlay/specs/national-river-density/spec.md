## ADDED Requirements

### Requirement: National river-network tiles use the denser stream_type threshold table
The national river-network MVT SQL SHALL filter `core.river_segment.stream_type` with the threshold table z≤4 → ≥4, z5 → ≥3, z6 → ≥2, z7 → ≥1, z≥8 → ≥1 (one class denser than the previous z≤4 → 5 / z5 → 4 / z6 → 3 / z7 → 2 table), and `NATIONAL_RIVER_NETWORK_QUERY_VERSION` SHALL be bumped to `stream-type-aggregate-v3` so cached tiles are regenerated. The threshold lives in ONE `CASE` expression inside the single SQL string that `postgis_tile_sql(layer: str) -> str` returns for `"river-network-national"`; zoom is a SQL bind (`:z`), not a Python argument, so the assertion seam is substring matching on that one string. The identically shaped `CASE` inside the `hydro-national` SQL is NOT changed by this requirement. The `CASE` is today built once in a `source_cte` shared by BOTH `"river-network"` (per-basin, per-segment, no aggregation) and `"river-network-national"` (the `if layer in {"river-network", "river-network-national"}` branch), so the v3 literals MUST be applied only when `layer == "river-network-national"`; the per-basin `river-network` layer SHALL keep the v2 table unchanged, because at z7 it would otherwise emit the full basin network per tile and the binding limit there is `MVT_MAX_FEATURES` (10 000), not the coordinate budget: measured on node-27 over the three densest active networks at the z7 tolerance, the worst per-basin z7 tile grows from 5 998 to 10 870 features under Type>=1 (basins_jialingjiang_rivnet_vbasins, tile 101/52) while its measured coordinate total is 38 014. Both figures are lower bounds, since the measurement assigns each segment to the tile holding its centroid, so the feature count is what settles this: a lower bound of 10 870 already exceeds `MVT_MAX_FEATURES`, whereas no lower bound on the coordinate total can show it stays under the coordinate budget. The feature count alone puts that tile over the budget. Those three-network numbers are recorded in the appendix of the same receipt; nothing in the national-layer go/no-go sweep itself covers this layer.

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

### Requirement: National river tiles carry a fair collection coordinate budget
The `river-network-national` SQL SHALL bound its own collection coordinate total the way `hydro-national`
already does, instead of letting an over-budget tile become an HTTP 413. A `national_budget_window` CTE
SHALL rank the eligible rows so that every network contributes its strongest remaining stream class before
any network contributes its second, and SHALL admit rows only while the running coordinate total stays
within the layer's collection limit.

The window SHALL be built on top of the layer's EXISTING eligibility filter, not on `bounded_rows`: the
current `eligible` body (`source_coordinate_count <= :feature_coordinate_limit AND
source_coordinate_dimensions <= :max_coordinate_dimensions`) becomes `preeligible`, exactly as
`hydro-national` does, so the per-feature and dimension guards keep applying before ranking.

Every `"Type" DESC` ordering the window introduces SHALL carry `NULLS LAST`. `core.river_segment.stream_type`
is nullable and the `z >= 9` arm deliberately admits rows regardless of stream class (`:z >= 9 OR
rs.stream_type >= ...`), so under Postgres' `DESC` default of `NULLS FIRST` an unclassified segment would
outrank every trunk and consume the budget first. `hydro-national` orders `value DESC NULLS LAST` for the
same reason. There are zero NULL stream classes in today's active networks, so this is a latent defect, not
a live one.

All THREE ORDER BY clauses the window introduces SHALL end with `river_segment_id` as a unique tiebreak:
the `network_rank` `ROW_NUMBER()`, the `tile_feature_rank` `ROW_NUMBER()`, and the `tile_coordinate_rank`
`SUM() OVER`. The `network_rank` clause matters most, because a non-unique rank assignment there changes
which rows the outer global order even considers. This is not
cosmetic. The same SQL string serves two row shapes: at `z <= 8` the rows are merged features, one per
`(river_network_version_id, basin_version_id, "Type")`, so a network has at most five rows and `"Type"` is
already unique within a network; at `z >= 9` the UNION ALL arm emits per-segment rows where `"Type"` has
massive ties. Without a unique tiebreak the truncation point at `z >= 9` depends on the execution plan, so
two runs can drop different features and the non-deterministic result is written into `map.tile_cache` and
the file cache under one generation. `hydro-national` carries `river_segment_id` in both of its ORDER BY
clauses for this reason. The route accepts z up to 14 and the national river layer declares `min_zoom: 0`
with no `max_zoom`, so the `z >= 9` path is reachable.

Applying the window changes `z >= 9` national river behaviour from HTTP 413 on an over-budget tile to a
deterministic truncation. That is the intended trade and SHALL be recorded in the receipt.

The per-feature limit is unchanged and stays `MVT_MAX_COORDINATES` (50 000) for every layer; only the
collection limit becomes layer-specific, and only `river-network-national` receives a raised value
(120 000). The per-basin `river-network` layer, the `hydro` layer, the `hydro-national` layer and the
`met-stations` layer SHALL keep both limits at `MVT_MAX_COORDINATES`. The per-basin `river-network` layer
SHALL NOT receive the window at all; its per-segment semantics are unchanged.

Rationale, measured on node-27 over all 516 China tiles at z3/z4/z5/z6/z7 (see the receipt): the v3
threshold table puts exactly one tile per zoom above 50 000 collection coordinates (86 160 / 52 458 /
53 432 / 56 354 / 52 381) while every single feature stays far below the per-feature limit (max 19 528,
zero overflow). Without this requirement those five tiles return HTTP 413 and the v3 table cannot ship.
Raising the global `MVT_MAX_COORDINATES` was rejected: the same constant is the per-feature guard, the
collection guard and the 413 threshold for all five tile layers, so a global raise also loosens
`hydro-national`'s fair budget window and the per-basin layer's truncation behaviour.

#### Scenario: Over-budget tiles degrade instead of failing
- **WHEN** a `river-network-national` tile's eligible rows total more coordinates than the layer's collection limit
- **THEN** the SQL admits rows in rank order until the limit is reached and drops the remainder
- **AND** the route returns a rendered tile rather than HTTP 413 `MVT_TILE_BUDGET_EXCEEDED`

#### Scenario: Networks are served round-robin by rank, not first-come
- **WHEN** the budget window ranks the eligible rows of one tile
- **THEN** no network holds an admitted row of `network_rank = k + 1` while another network's row of `network_rank = k` was dropped
- **AND** within one `network_rank` group the admitted rows are ordered by `"Type"` descending, so the rows dropped inside a group are its lowest stream classes

#### Scenario: Truncation is deterministic at per-segment zooms
- **WHEN** the national river SQL is generated
- **THEN** the `network_rank` `ROW_NUMBER()` ORDER BY and both `national_budget_window` ORDER BY clauses each end with `river_segment_id`
- **AND** each of those three clauses orders `"Type" DESC NULLS LAST`, never a bare `"Type" DESC`
- **AND** generating a `z >= 9` tile twice against unchanged data yields byte-identical output

#### Scenario: The window is built on the existing eligibility filter
- **WHEN** the national river SQL is generated
- **THEN** the ranked input is a `preeligible` CTE carrying `source_coordinate_count <= :feature_coordinate_limit` and `source_coordinate_dimensions <= :max_coordinate_dimensions`
- **AND** `postgis_tile_sql("river-network")` contains no `national_budget_window`, no `network_rank` and no `preeligible`

#### Scenario: Only the national river layer gets the raised collection limit
- **WHEN** the tile parameters are built for each layer
- **THEN** `river-network-national` receives `collection_coordinate_limit = 120000`
- **AND** `river-network`, `hydro`, `hydro-national` and `met-stations` each receive `collection_coordinate_limit = MVT_MAX_COORDINATES`
- **AND** all five layers, `river-network-national` included, receive `feature_coordinate_limit = MVT_MAX_COORDINATES`
- **AND** a caller that supplies no layer still receives `collection_coordinate_limit = MVT_MAX_COORDINATES`, because existing script and test callers compare the exact binding dictionary

#### Scenario: The 413 contract is preserved at the new limit
This scenario is defence in depth. For `river-network-national` it is unreachable through the real SQL once
the window lands, because `eligible` is a prefix of `tile_coordinate_rank` and `tile_feature_rank`, so
`budget_stats` can never exceed either limit. It still binds the route's behaviour for the unwindowed
layers and against a future SQL change, and is exercised with stubbed rows.

- **WHEN** a tile exceeds the layer's own collection limit or the feature limit
- **THEN** the route still raises HTTP 413 `MVT_TILE_BUDGET_EXCEEDED` against that layer's limit, not against a hard-coded 50 000
- **AND** the error `details.max_coordinates` reports that layer's limit, not `MVT_MAX_COORDINATES`

### Requirement: Denser national river tiles stay within the coordinate budget
Before the threshold change is enabled in production, node-27 SHALL measure the coordinate budget of every
z3, z4, z5, z6 and z7 tile intersecting the China bounds with the new SQL. z5 is included because the v3
table also moves it (>=4 to >=3); z>=8 is excluded because the v2 table already reads `ELSE 1.0` there.
The measured tile set SHALL be the one produced by `scripts/node27_mvt_prewarm.py::xyz_tiles(CHINA_BOUNDS,
[3, 4, 5, 6, 7])` (516 tiles), so two runs enumerate the same tiles.

Three metrics SHALL be recorded per tile, before and after the change:
`prefilter_stats.feature_coordinate_count` (the MAXIMUM single-feature coordinate count),
`prefilter_stats.feature_coordinate_overflow_count`, and `budget_stats.coordinate_count`. All three are
hard conditions, and they fail differently:

- `feature_coordinate_count >= feature_coordinate_limit` or `feature_coordinate_overflow_count > 0` empties
  `budget_gate`, so the route returns HTTP 200 with an EMPTY body which is then cached. It is a silent
  blank tile, not an error.
- `budget_stats.coordinate_count` above the layer's collection limit is the route's own 413 condition
  (`apps/api/routes/hydro_display.py`, the `MVT_TILE_BUDGET_EXCEEDED` branch). It is a hard request failure.

`budget_stats.coordinate_count` is therefore NOT sufficient on its own (it sums only the features that
already passed the per-feature limit, so it reads low exactly when a merged trunk blew that limit and was
filtered out), but exceeding it is NOT acceptable either. The receipt SHALL additionally record
`length(tile)` per tile, because a non-empty tile body is the single direct observable that subsumes
per-feature overflow, collection over-budget and invalid-property failures.

Every measured tile MUST satisfy all three: `feature_coordinate_count < MVT_MAX_COORDINATES` (50 000),
`feature_coordinate_overflow_count == 0`, and `budget_stats.coordinate_count <=` the layer's collection
limit.

Once the budget window lands, the third condition holds by construction (`tile_coordinate_rank` is a
monotonic running total and `eligible` takes a prefix of it), so on its own it would no longer discriminate
anything. The measurement SHALL therefore run every tile TWICE: once with the production binds, and once
with `:collection_coordinate_limit` bound to an effectively unbounded value (1e9). A fourth recorded
quantity per tile is the untruncated `coordinate_count` from the second run. Go additionally requires the
two runs to report the SAME `coordinate_count` for every tile, which is the only evidence that the window
did not silently drop features at the 120 000 boundary; a tile where they differ is truncating and MUST be
recorded as such in the receipt with both values. If any tile still fails, the affected zoom level MUST fall back one class and the
receipt MUST record the measured values and the fallback. A fallback is a change to the SQL shape, so it
MUST also update the threshold table in the first Requirement, both literal lists in its first scenario,
the same table in `design.md` section D7, and the assertions in `tasks.md` 2.2, in the same PR; and any SQL-shape change made after tiles have
already been served under a generation requires a further `NATIONAL_RIVER_NETWORK_QUERY_VERSION` bump.

Production has exactly ONE bind site for tile SQL, in `apps/api/routes/hydro_display.py`'s
`_fetch_postgis_tile_bytes`, where `postgis_tile_sql(layer)` and `_postgis_tile_params(...)` are passed to
`session.execute`. That call SHALL forward the layer. If it does not, `:collection_coordinate_limit` binds
to 50 000 while the 413 comparison uses 120 000: the window truncates every hot tile at the wrong point,
nothing raises, no tile is empty, and the density goal of this change is silently lost in production while
every local and measured check passes. A test SHALL capture the parameters actually bound by that call and
assert 120 000 for `river-network-national` and `MVT_MAX_COORDINATES` for the other four layers.

The measurement SHALL bind the SQL exactly as production does, by calling
`apps/api/routes/hydro_display.py::_postgis_tile_params(..., layer="river-network-national")` rather than
retyping the binds. Two binds each silently invalidate the result if wrong.
`feature_coordinate_overflow_count` and `budget_stats.coordinate_count` are both defined against
`:feature_coordinate_limit`, so binding it to anything other than `MVT_MAX_COORDINATES` makes both metrics
meaningless. And after the collection limit becomes layer-specific, omitting the `layer` argument silently
binds `:collection_coordinate_limit` to 50 000, which truncates the window at the wrong point and produces a
receipt where every tile passes for the wrong reason. The receipt SHALL therefore record the actual bound
value of `collection_coordinate_limit`.
`:simplification_tolerance_m` does not affect any of the three metrics (it applies in the `simplified`
CTE, downstream of `bounded_rows`); the generalisation that does drive `ST_NPoints` is the hardcoded
tolerance CASE inside `source_cte`.

The "before" numbers SHALL be produced by running the pre-change SQL from a checkout of the change's merge
base -- or of any commit whose tile-SQL sources are byte-identical to it, with that emptiness recorded in
the receipt -- and the "after" numbers from a checkout at the change head, against the same database. Substituting
the four threshold literals inside the post-change SQL string SHALL NOT be used: once the collection budget
window lands, that substituted string still contains the window, so its `coordinate_count` is capped at the
bound limit and the before-numbers are wrong for exactly the tiles that were failing. The receipt SHALL
record the measurement commit SHA, the baseline SHA, and a content anchor that survives a rebase -- the
generated SQL digest of every layer -- so a reader can confirm that no layer other than the one under test
moved between the two runs.
These columns are outputs of the tile SQL (`services/tiles/mvt.py`, final `SELECT`) and are read by
running that SQL directly against node-27's database, not from the tile HTTP route, which does not expose
them.

#### Scenario: Go decision
- **WHEN** every z3, z4, z5, z6 and z7 China tile measures `feature_coordinate_count < 50 000`, `feature_coordinate_overflow_count == 0`, `coordinate_count` within the layer's collection limit, and the same `coordinate_count` under the unbounded-limit run
- **THEN** the v3 threshold table ships unchanged
- **AND** the receipt lists, per tile and per zoom, `feature_coordinate_count`, `feature_coordinate_overflow_count`, `coordinate_count`, the untruncated `coordinate_count` and `length(tile)`, before and after the change

#### Scenario: No-go decision
- **WHEN** any measured tile has a single feature at or above 50 000 coordinates, or reports `feature_coordinate_overflow_count > 0`, or a `coordinate_count` above the layer's collection limit
- **THEN** that zoom level's threshold is raised by one class in the shipped table, together with the first Requirement's table, its scenario literal lists and the `tasks.md` 2.2 assertions
- **AND** the receipt and the spec comment record which level was rolled back and why

#### Scenario: Overflow is not masked by the aggregate
- **WHEN** a tile reports `feature_coordinate_overflow_count > 0` while `budget_stats.coordinate_count` is small or zero because the oversized feature was filtered out
- **THEN** the decision is still No-go for that zoom level
- **AND** the receipt records that the aggregate was misleading for that tile, and that `length(tile)` for that tile is zero

#### Scenario: Silent truncation at the collection limit is detected
- **WHEN** a tile is measured once with the production binds and once with `:collection_coordinate_limit` bound to 1e9
- **THEN** Go requires the two runs to report the same `budget_stats.coordinate_count`
- **AND** a tile where they differ is recorded in the receipt as truncating, with both values, because the window dropped features the denser table was supposed to add

#### Scenario: Per-segment zooms are spot-checked for determinism
- **WHEN** the receipt is produced
- **THEN** it records at least two `z >= 9` national river tiles, each generated twice by running the SQL directly rather than through the route, since a second route request would be served from `map.tile_cache` or the file cache and prove nothing
- **AND** the sampled tiles are chosen from the unbounded-limit run as ones that actually truncate, having `coordinate_count` above the collection limit or `feature_count` at the feature limit
- **AND** the two generations of each sampled tile are byte-identical
- **AND** if no `z >= 9` tile truncates at the layer's production collection limit, the receipt SHALL instead force the
  truncation path by binding `:collection_coordinate_limit` below a sampled tile's own total, generate each forced
  case under more than one execution-plan shape, and record the digests; a receipt that records "no valid sample" is
  conforming only when even the forced construction is unavailable, and reporting a pass from untruncated tiles alone
  is never conforming

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
- **AND** `Type = 4` at zoom 5 is ≥ 2.2 px (it is ≈1.86 px today, so an unchanged paint fails this assertion)
- **AND** `Type = 5` is > 1.5 px at zoom 3 and > 2.3 px at zoom 5 (it is exactly 1.5 px and ≈2.23 px today), so the stops must strictly increase rather than merely satisfy the old values
