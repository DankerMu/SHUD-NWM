## Why

M10/M11 intentionally kept GeoJSON as a compatibility path for flood/overview maps. Final acceptance requires national-scale hydrology rendering through vector tiles rather than full GeoJSON loads, with PostGIS clipping, MVT/PBF contracts, frontend consumption, and performance evidence.

## What Changes

- Define exact canonical MVT endpoints for `/api/v1/tiles/river-network/{basin_version_id}/{z}/{x}/{y}.pbf`, `/api/v1/tiles/hydro/{run_id}/{variable}/{valid_time}/{z}/{x}/{y}.pbf`, and true `/api/v1/tiles/flood-return-period/{run_id}/{duration}/{valid_time}/{z}/{x}/{y}.pbf`, with `application/x-protobuf`, stable layer IDs, required feature properties, and bounded query parameters. Keep `/api/v1/tiles/flood-return-period` as the bounded GeoJSON compatibility endpoint.
- Implement PostGIS tile clipping/simplification, z/x/y and bbox validation, map.tile_layer/map.tile_cache metadata, caching/invalidation, stable error codes, and feature/coordinate budgets.
- Migrate frontend MapLibre hydrology layers to vector tile sources when MVT is available, preserving bounded GeoJSON fallback for small/degraded views.
- Add national performance evidence: PostGIS query plan/hash, p95 latency, payload size, memory, browser timing, tile count, and no full-national GeoJSON assertions.
- Keep meteorology raster/grid product delivery separate from hydrology vector MVT.

## Capabilities

### New Capabilities

- `mvt-tile-contract`
- `postgis-tile-clipping-cache`
- `frontend-mvt-layer-consumption`
- `national-performance-evidence`
- `mvt-fallback-compatibility`

## Impact

- Backend tile APIs, OpenAPI, map.tile_layer/map.tile_cache, tile publisher, frontend MapLibre layers, production closure/scale validation.

## Non-Goals

- Meteorology raster products; those are M13.
- Final live production readiness without target-environment proof.
- Unbounded national GeoJSON as primary rendering path.
