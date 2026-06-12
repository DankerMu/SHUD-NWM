## Context

Fixture level: expanded

Repair intensity: high

Project profile: NHMS

Issue #342 adds the backend half of station-MVT. Existing MVT routes for river network, hydro, hydro-national, and flood-return-period live in `apps/api/routes/flood_alerts.py` and delegate SQL/cache behavior to `services/tiles/mvt.py`. Station inventory routes already exist at `/api/v1/met/stations`, but that JSON contract is not an efficient nationwide point overlay contract.

## Goals / Non-Goals

Goals:
- Add a display-readonly-compatible station point MVT route under `/api/v1/tiles/met-stations/...`.
- Encode station features with stable source-layer and property names: `station_id`, station identity, basin/model binding where available, role/name, and active flag.
- Keep live PostGIS gating, tile cache headers, byte/feature/coordinate budgets, and error behavior consistent with existing canonical `.pbf` routes.
- Update OpenAPI and backend contract tests so generated consumers can discover the route later.

Non-goals:
- No frontend or node-27 consumer migration in this PR.
- No station clustering, styling, or MapLibre source changes.
- No station table schema migration or station seeding changes.
- No promise that local sqlite tests produce real MVT bytes; PostGIS live tile retrieval remains a node-22/live-DB oracle.

## Decisions

1. Reuse the existing canonical MVT route pattern.
   - `read_cached_tile_response`, `_require_live_postgis_mvt`, `_fetch_postgis_tile_bytes`, `build_raw_tile_response`, and `_mvt_response` should remain the public route flow.
   - Alternative rejected: hand-encoding JSON/GeoJSON or deterministic test tiles for the public route, because `.pbf` routes must be canonical PostGIS MVT.

2. Use a basin-version scoped route first.
   - Route shape: `/api/v1/tiles/met-stations/{basin_version_id}/{z}/{x}/{y}.pbf`.
   - The issue requested basin-scoped station tiles as the frontend handoff primitive. A later national-union route can be added once frontend behavior and DB indexing evidence are known.

3. Bind source identity to station inventory, not forecast run identity.
   - `source_id` for tile cache should be the requested `basin_version_id`.
   - `source_version` should be derived deterministically from station rows for that basin version, so cache invalidates when station inventory changes.

4. Keep SQL bounded and property-safe.
   - Use `ST_AsMVTGeom` on station point geometry.
   - Enforce feature budgets and required property checks before returning bytes.
   - Empty identity must return the same stable `MVT_LIVE_POSTGIS_UNAVAILABLE` family as existing canonical MVT routes.

5. Add a forward active-station source index for production boundedness.
   - Migration `000033_station_mvt_active_source_index.sql` adds `met_station_active_basin_station_idx` on `(basin_version_id, station_id) WHERE active_flag = true`.
   - The index matches the station source-version preflight query shape: basin-scoped active rows ordered by `station_id` with `LIMIT max_features + 1`.
   - This is an index-only forward migration; it does not change station table columns, seed data, or frontend behavior.

## Risks / Trade-offs

- Station table names may differ between reduced fixtures and production. Mitigation: implement against the documented `met.met_station` inventory and fail with stable live-MVT unavailable/error when absent; do not fabricate fallback tiles.
- Nationwide scale may still exceed a single basin tile in dense areas. Mitigation: reuse existing tile feature/byte budgets and cache; frontend can request by basin version first.
- OpenAPI drift can break generated clients. Mitigation: update static OpenAPI and runtime patching/tests together.
- Frontend is not updated here. Mitigation: expose a stable backend contract and leave node-27 migration to its own issue.

## Risk Packs Considered

- Public API / CLI / script entry: selected - new public tile route and OpenAPI path.
- Config / project setup: selected - route depends on existing `NHMS_ENABLE_LIVE_POSTGIS_MVT=true` gate, no new env.
- File IO / path safety / overwrite: not selected - no file paths or publish writes.
- Schema / columns / units / field names: selected - station MVT property names and source-layer contract are new.
- Auth / permissions / secrets: not selected - readonly tile route only, no credentials or auth changes.
- Concurrency / shared state / ordering: selected - tile cache identity and station source version must be deterministic.
- Resource limits / large input / discovery: selected - point tiles must respect feature/byte budgets.
- Legacy compatibility / examples: selected - existing MVT routes and station JSON APIs must not change.
- Error handling / rollback / partial outputs: selected - missing live PostGIS/table/identity must fail with stable API errors.
- Release / packaging / dependency compatibility: not selected - no dependency change.
- Documentation / migration notes: selected - OpenAPI is the source of truth for later node-27/frontend migration.

Domain packs:
- Geospatial / CRS / basin geometry: selected - point geometry must be transformed/clipped to Web Mercator tile bounds.
- Hydro-met time series / forcing windows: not selected - no time-series values or forcing windows in tile payload.
- SHUD numerical runtime / conservation / NaN: not selected.
- PostGIS / TimescaleDB domain behavior: selected - route relies on PostGIS functions and schema/table behavior.
- Slurm production lifecycle / mock-vs-real parity: not selected.
- External hydro-met providers / snapshot reproducibility: not selected.
- Run manifest / QC provenance: not selected.
- Published NHMS artifacts / display identity: selected - this is a display tile contract consumed later by node-27/frontend.

## Invariant Matrix

Governing invariant: a station MVT tile must represent only station points for the requested basin-version identity, with stable MVT layer/properties, bounded resource use, and no JSON/GeoJSON fallback masquerading as `.pbf`.

Source-of-truth identity/contract: `basin_version_id`, station inventory rows, `station_id`, geometry, `MVT_SCHEMA_VERSION`, `MVT_ENCODER_VERSION`, tile XYZ, and live PostGIS MVT gate.

Surfaces:
- Producers: `services/tiles/mvt.py::postgis_tile_sql`, station source-version helper.
- Validators/preflight: `validate_identifier`, `validate_xyz`, `_require_live_postgis_mvt`.
- Storage/cache/query: `map.tile_cache`, `map.tile_layer`, `met.met_station` reads.
- Production boundedness: `met_station_active_basin_station_idx` on active basin/station source lookup.
- Public routes/entrypoints: `/api/v1/tiles/met-stations/{basin_version_id}/{z}/{x}/{y}.pbf`.
- Frontend/downstream consumers: OpenAPI/generated consumers only; frontend migration out of scope.
- Failure paths/rollback/stale state: missing live gate, invalid XYZ, missing station identity/table, over-budget tile, invalid required property.
- Evidence/audit/readiness: OpenAPI contract, route tests, SQL tests, cache headers.

Regression rows:
- Valid basin version + live PostGIS enabled + station points in tile -> `.pbf` response with `application/x-protobuf`, cache headers, and source layer `met_stations`.
- Invalid XYZ or basin identifier -> stable 422.
- Live PostGIS disabled -> stable `MVT_LIVE_POSTGIS_UNAVAILABLE`.
- No source rows for basin version -> stable unavailable/not-found style error, not empty success that claims readiness.
- Over-budget or invalid required property -> stable MVT error.
- Existing hydro/flood/river MVT routes and `/api/v1/met/stations` JSON contract remain unchanged.
