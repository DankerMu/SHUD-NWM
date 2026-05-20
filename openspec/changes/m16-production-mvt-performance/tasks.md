## 1. Tile Contract
- [ ] 1.1 Define MVT endpoint paths, query parameters, `application/x-protobuf`, vector layer IDs, required feature properties, bounds, validation errors, cache headers, and OpenAPI contract. Expected output: OpenAPI and any generated frontend API types are fresh, and tests cover success/error content types.
- [ ] 1.2 Add or confirm `map.tile_layer` / `map.tile_cache` metadata requirements for URL templates, layer/source/run/version/valid_time keys, checksum/etag, status, encoder/schema version, and invalidation. Expected output: schema/migration/tests prove stable metadata and do not break existing seed/migration tests.
- [ ] 1.3 Implement exact path disposition: canonical MVT paths are `/api/v1/tiles/river-network/{basin_version_id}/{z}/{x}/{y}.pbf`, `/api/v1/tiles/hydro/{run_id}/{variable}/{valid_time}/{z}/{x}/{y}.pbf`, and true `/api/v1/tiles/flood-return-period/{run_id}/{duration}/{valid_time}/{z}/{x}/{y}.pbf`; `/api/v1/tiles/flood-return-period` remains bounded GeoJSON compatibility. Expected output: tests prove legacy flood `.pbf` no longer redirects once promoted, or remains explicitly compatibility-only if not promoted.
- [ ] 1.4 Define the frontend layer metadata discovery contract with `layer_id`, `tile_format`, URL template placeholders, MapLibre `source-layer`, property schema/version, min/max zoom, Web Mercator bounds, valid_times/source refs, cache etag/version, and fallback/release-blocking flags.

## 2. Backend Implementation
- [ ] 2.1 Implement PostGIS-oriented z/x/y tile envelope clipping and simplification helpers using bounded SQL shapes (`ST_TileEnvelope`, clipping, `ST_AsMVTGeom`, `ST_AsMVT` or an implementation-equivalent abstraction), feature/coordinate/payload budgets, stable validation errors that fail before expensive SQL, and a production hydro MVT identity lookup index whose leading columns match `(run_id, variable, valid_time)`.
- [ ] 2.2 Implement cache read/write/idempotency and invalidation behavior with tests for cache hit, miss, repeat request, layer/version/valid_time invalidation, checksum/etag identity, and cache failure fallback.
- [ ] 2.3 Add focused unit/integration tests for MVT SQL builder/cache paths and real PostGIS opt-in coverage that is skipped/not_executed when unavailable without claiming readiness.
- [ ] 2.4 Add oversized tile and non-finite/invalid property handling tests so degraded/error responses do not emit misleading PBF or unbounded FeatureCollections.
- [ ] 2.5 Add CRS/tile-matrix tests for Web Mercator XYZ semantics: z/x/y bounds, source CRS transform before encoding, extent/buffer, simplification tolerance, geometry type handling, and encoded coordinate bounds.

## 3. Frontend Consumption
- [ ] 3.1 Update MapLibre hydrology/flood-return-period layers to consume MVT layer metadata when available. Expected output: vector sources/layers use stable layer IDs and tile URL templates.
- [ ] 3.2 Preserve bounded GeoJSON fallback for small/degraded views with explicit UI status and request limits.
- [ ] 3.3 Add E2E/unit assertions that national hydrology views do not fetch full-national GeoJSON when MVT is available and show release-blocking/unavailable status when national MVT is required but unavailable.
- [ ] 3.4 Preserve M11-M15 URL restore, valid-time/timeline behavior, RBAC navigation behavior, and visual evidence compatibility.

## 4. Performance Evidence
- [ ] 4.1 Add deterministic large-fixture validation for SQL shape/plan hash, p95 latency, payload size, tile count, feature/coordinate counts, memory proxy, browser timing, thresholds, and artifact paths.
- [ ] 4.2 Add real-data opt-in evidence mode without claiming final readiness when PostGIS/national data/dependencies are missing. Expected output: current `ready`/`blocked` and `production_mvt_readiness_claimed` semantics remain compatible; missing live dependency records `not_executed` or `release_blocked` details plus blockers while top-level production MVT readiness remains not claimed.
- [ ] 4.3 Update validation docs and `progress.md` with MVT readiness, GeoJSON compatibility boundary, deterministic/live evidence interpretation, and remaining live-data limits.
- [ ] 4.4 Run OpenSpec strict validation, affected backend tests, OpenAPI drift/type checks, affected frontend tests/build/e2e, production scale validation tests, and `uv run ruff check .` or narrower ruff scope justified by changed files.

## 5. Non-Goals and Guardrails
- [ ] 5.1 Do not claim final live production readiness without target-environment PostGIS/national-data proof.
- [ ] 5.2 Do not make full-national GeoJSON the primary national rendering path.
- [ ] 5.3 Do not change meteorology raster/grid products as part of hydrology MVT work.
- [ ] 5.4 Do not remove existing bounded GeoJSON compatibility tests unless replaced by stronger MVT+fallback coverage.
