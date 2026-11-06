## MODIFIED Requirements

### Requirement: National river tiles carry a fair collection coordinate budget

The `river-network-national` SQL SHALL bound its own collection coordinate total the way `hydro-national` already does, instead of letting an over-budget tile
become an HTTP 413. A `national_budget_window` CTE SHALL rank the eligible rows so that every network contributes its strongest remaining stream class before
any network contributes its second, and SHALL admit rows only while the running coordinate total stays within the layer's collection limit.

The window SHALL be built on top of the layer's EXISTING eligibility filter, not on `bounded_rows`: the current `eligible` body
(`source_coordinate_count <= :feature_coordinate_limit AND source_coordinate_dimensions <= :max_coordinate_dimensions`) becomes `preeligible`, exactly as
`hydro-national` does, so the per-feature and dimension guards keep applying before ranking.

Every `"Type" DESC` ordering the window introduces SHALL carry `NULLS LAST`. `core.river_segment.stream_type` is nullable and the `z >= 9` arm deliberately
admits rows regardless of stream class (`:z >= 9 OR rs.stream_type >= ...`), so under Postgres' `DESC` default of `NULLS FIRST` an unclassified segment would
outrank every trunk and consume the budget first. `hydro-national` orders `value DESC NULLS LAST` for the same reason. There are zero NULL stream classes in
today's active networks, so this is a latent defect, not a live one.

All THREE ORDER BY clauses the window introduces SHALL end with `river_segment_id` as a unique tiebreak: the `network_rank` `ROW_NUMBER()`, the
`tile_feature_rank` `ROW_NUMBER()`, and the `tile_coordinate_rank` `SUM() OVER`. The `network_rank` clause matters most, because a non-unique rank assignment
there changes which rows the outer global order even considers. This is not cosmetic. The same SQL string serves two row shapes: at `z <= 8` the rows are merged
features, one per `(river_network_version_id, basin_version_id, "Type")`, so a network has at most five rows and `"Type"` is already unique within a network; at
`z >= 9` the UNION ALL arm emits per-segment rows where `"Type"` has massive ties. Without a unique tiebreak the truncation point at `z >= 9` depends on the
execution plan, so two runs can drop different features and the non-deterministic result is written into `map.tile_cache` and the file cache under one
generation. `hydro-national` carries `river_segment_id` in both of its ORDER BY clauses for this reason. The route accepts z up to 14 and the national river
layer declares `min_zoom: 0` with no `max_zoom`, so the `z >= 9` path is reachable.

Applying the window changes `z >= 9` national river behaviour from HTTP 413 on an over-budget tile to a deterministic truncation. That is the intended trade and SHALL be recorded in the receipt.

The per-feature limit is unchanged and stays `MVT_MAX_COORDINATES` (50 000) for every layer; only the collection limit becomes layer-specific, and only
`river-network-national` receives a raised value (120 000). The per-basin `river-network` layer, the `hydro` layer and the `hydro-national` layer SHALL keep
both limits at `MVT_MAX_COORDINATES`. The per-basin `river-network` layer SHALL NOT receive the window at all; its per-segment semantics are unchanged.

Rationale, measured on node-27 over all 516 China tiles at z3/z4/z5/z6/z7 (see the receipt): the v3 threshold table puts exactly one tile per zoom above 50 000
collection coordinates (86 160 / 52 458 / 53 432 / 56 354 / 52 381) while every single feature stays far below the per-feature limit (max 19 528, zero
overflow). Without this requirement those five tiles return HTTP 413 and the v3 table cannot ship. Raising the global `MVT_MAX_COORDINATES` was rejected: the
same constant is the per-feature guard, the collection guard and the 413 threshold for every tile layer, so a global raise also loosens `hydro-national`'s fair
budget window and the per-basin layer's truncation behaviour.

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
- **AND** `river-network`, `hydro` and `hydro-national` each receive `collection_coordinate_limit = MVT_MAX_COORDINATES`
- **AND** all four layers, `river-network-national` included, receive `feature_coordinate_limit = MVT_MAX_COORDINATES`
- **AND** a caller that supplies no layer still receives `collection_coordinate_limit = MVT_MAX_COORDINATES`, because existing script and test callers compare the exact binding dictionary

#### Scenario: The 413 contract is preserved at the new limit

This scenario is defence in depth. For `river-network-national` it is unreachable through the real SQL once the window lands, because `eligible` is a prefix of
`tile_coordinate_rank` and `tile_feature_rank`, so `budget_stats` can never exceed either limit. It still binds the route's behaviour for the unwindowed layers
and against a future SQL change, and is exercised with stubbed rows.

- **WHEN** a tile exceeds the layer's own collection limit or the feature limit
- **THEN** the route still raises HTTP 413 `MVT_TILE_BUDGET_EXCEEDED` against that layer's limit, not against a hard-coded 50 000
- **AND** the error `details.max_coordinates` reports that layer's limit, not `MVT_MAX_COORDINATES`
