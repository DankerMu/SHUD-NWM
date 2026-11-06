## MODIFIED Requirements

### Requirement: The national discharge layer has its own feature budget

`services/tiles/mvt.py::feature_limit(layer)` SHALL return `NATIONAL_DISCHARGE_FEATURE_LIMIT` (20 000) for `hydro-national` and `MVT_MAX_FEATURES` for every
other layer and for `None`. `_postgis_tile_params` SHALL bind `:feature_limit` from it, and `_fetch_postgis_tile_bytes` SHALL use the same value for its 413
predicate, the 413 `max_features` detail and the `MVT_TILE_BUDGET_TRUNCATED` `max_features` field. Because this changes `hydro-national` tile bytes wherever
more than 10 000 features intersect, `NATIONAL_DISCHARGE_QUERY_VERSION` SHALL be `fair-network-budget-v6`.

#### Scenario: A national discharge tile above the old limit is no longer truncated

- **WHEN** a `hydro-national` tile intersects 13 770 features and no coordinate limit is reached
- **THEN** all 13 770 are selected and no `MVT_TILE_BUDGET_TRUNCATED` record is emitted

#### Scenario: The fair window still truncates above the new limit, observably

- **WHEN** a `hydro-national` tile intersects 21 000 features
- **THEN** 20 000 are selected and one `MVT_TILE_BUDGET_TRUNCATED` record carries `max_features=20000`

#### Scenario: Every other layer keeps the global feature budget

- **WHEN** `_postgis_tile_params` is built for `river-network`, `river-network-national` or `hydro`
- **THEN** its `feature_limit` equals `MVT_MAX_FEATURES`
