## 1. MVT Helper Contract

- [ ] 1.1 Add station layer support to `postgis_tile_sql()` with `met_stations` source-layer properties and PostGIS point geometry clipping.
- [ ] 1.2 Add station source-version/cache metadata helper so cache keys bind to `basin_version_id` and station inventory changes.
- [ ] 1.3 Preserve MVT budgets, invalid-property checks, cache read/write behavior, and existing route behavior.

## 2. API Route and OpenAPI

- [ ] 2.1 Add `/api/v1/tiles/met-stations/{basin_version_id}/{z}/{x}/{y}.pbf` route using existing live PostGIS gate and raw tile response flow.
- [ ] 2.2 Patch runtime OpenAPI and static `openapi/nhms.v1.yaml` with path, operation id, response headers, and error responses.
- [ ] 2.3 Add layer metadata only if needed for discoverability without changing existing frontend behavior.

## 3. Regression and Contract Tests

- [ ] 3.1 Add SQL tests proving station MVT uses `ST_AsMVTGeom`, `met.met_station`, source layer `met_stations`, and required station properties.
- [ ] 3.2 Add route tests for invalid XYZ/identifier and live PostGIS disabled behavior.
- [ ] 3.3 Add route/cache tests with mocked PostGIS result proving `.pbf` response headers, cache miss/hit, and source identity binding.
- [ ] 3.4 Add OpenAPI drift/contract tests for runtime/static path parity.
- [ ] 3.5 Re-run existing MVT and station API tests to prove compatibility.

## 4. Verification

- [ ] 4.1 Run focused MVT/API tests, including `tests/test_flood_alerts_api.py` and OpenAPI drift coverage.
- [ ] 4.2 Run focused ruff on touched backend/API/test files.
- [ ] 4.3 Run `openspec validate issue-342-station-mvt-tiles --strict --no-interactive`.
- [ ] 4.4 If live PostGIS is unavailable locally, document the node-22 oracle command and keep local tests at SQL/route/contract level.

## Evidence Mapping

| Invariant / Scenario | Task IDs | Required evidence |
|---|---:|---|
| Valid station tile returns canonical `.pbf` bytes, MVT headers, and `met_stations` source layer | 1.1, 2.1, 3.3, 4.1 | Route test with mocked PostGIS row returning bytes and asserting media type + headers; SQL test asserts `ST_AsMVT(..., 'met_stations', ...)`. |
| Live PostGIS gate prevents JSON/GeoJSON fallback on `.pbf` route | 2.1, 3.2, 4.1 | API test on sqlite or env-disabled session returns `MVT_LIVE_POSTGIS_UNAVAILABLE` and non-200 response. |
| Invalid XYZ and invalid `basin_version_id` fail stably | 2.1, 3.2, 4.1 | API tests assert 422 `TILE_XYZ_INVALID` / `VALIDATION_ERROR` without DB tile query. |
| Basin-version isolation and station properties are encoded from station inventory | 1.1, 3.1, 4.1 | SQL text test asserts `WHERE ... basin_version_id = :basin_version_id`, required property checks for `station_id`, `basin_version_id`, `station_role`, `active_flag`, and public tile columns. |
| Budget/property failures use canonical MVT error family | 1.1, 1.3, 3.1, 4.1 | SQL test asserts station layer participates in existing `feature_limit`, coordinate, and invalid-property counters; route helper test may mock overflow row to assert `MVT_TILE_BUDGET_EXCEEDED` / `MVT_PROPERTY_INVALID`. |
| Cache identity is stable and bound to station source version | 1.2, 2.1, 3.3, 4.1 | Route/cache test asserts first request writes/misses and second request hits for same basin/XYZ/schema; source-version helper test changes station inventory input and changes cache key/version. |
| Active station source-version lookup is production-indexed and bounded | 1.2, 1.3, 3.1, 4.1 | Forward migration test asserts `met_station_active_basin_station_idx` on `(basin_version_id, station_id) WHERE active_flag = true`; source-inventory tests assert over-limit failure before cache/live SQL. |
| Static and runtime OpenAPI remain aligned | 2.2, 3.4, 4.1 | `tests/test_openapi_drift.py` or equivalent asserts static/runtime path, parameters, response headers, media type, and operation id. |
| Existing MVT routes and station JSON APIs remain compatible | 1.3, 3.5, 4.1 | Focused existing tests for river/hydro/flood MVT SQL/routes plus `/api/v1/met/stations` contract pass unchanged. |
| Node-22 live PostGIS oracle remains explicit when local DB cannot produce real MVT | 4.4 | PR evidence documents the node-22 command or marks live validation pending/out-of-scope for local CI; no fake production-readiness claim. |
