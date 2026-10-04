## ADDED Requirements

### Requirement: Met station inventory by model does not depend on planner statistics

`GET /api/v1/met/stations` with `model_id` SHALL select stations from `met.met_station` filtered by membership in an uncorrelated scalar-array subquery over `met.interp_weight` for that model, and SHALL NOT join `met.met_station` to `met.interp_weight`. The COUNT and page statements SHALL share the same predicate. The returned stations, their order by `station_id`, pagination, `total_count` and the `variables` coverage filter SHALL be identical to the previous JOIN with DISTINCT form.

#### Scenario: Model and basin filter
- **WHEN** `list_met_stations` is called with `model_id` and `basin_version_id`
- **THEN** both statements filter `ms.station_id = ANY((SELECT array_agg(DISTINCT station_id) FROM met.interp_weight WHERE model_id = %s)::text[])`, contain no `JOIN met.interp_weight`, and the page statement keeps `ORDER BY ms.station_id LIMIT %s OFFSET %s`

#### Scenario: Variable coverage filter
- **WHEN** `variables` names N variables together with `model_id`
- **THEN** only stations carrying all N variables in that model's `interp_weight` rows are returned, and `filters_applied.variables` lists them
