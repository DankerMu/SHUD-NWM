# mvt-tile-contract Specification

## Purpose
TBD - created by archiving change m16-production-mvt-performance. Update Purpose after archive.
## Requirements
### Requirement: MVT tile API contract
The backend SHALL expose hydrology vector tile endpoints with `application/x-protobuf`, stable layer IDs, bounded z/x/y parameters, and documented feature properties.

#### Scenario: Canonical endpoint disposition
WHEN M16 is implemented
THEN `/api/v1/tiles/river-network/{basin_version_id}/{z}/{x}/{y}.pbf`, `/api/v1/tiles/hydro/{run_id}/{variable}/{valid_time}/{z}/{x}/{y}.pbf`, and true `/api/v1/tiles/flood-return-period/{run_id}/{duration}/{valid_time}/{z}/{x}/{y}.pbf` have explicit OpenAPI/runtime behavior, while `/api/v1/tiles/flood-return-period` remains bounded GeoJSON compatibility

#### Scenario: Tile success
WHEN a published layer/run/valid_time has features in a tile
THEN endpoint returns PBF with required properties and cacheable headers

#### Scenario: Invalid tile
WHEN z/x/y or query parameters are out of bounds
THEN endpoint returns stable validation error without running expensive SQL

#### Scenario: Contract freshness
WHEN the public tile contract changes
THEN OpenAPI, generated frontend API types, and drift allowlists are updated together or the unchanged legacy path remains explicitly documented

#### Scenario: Stable feature properties
WHEN a hydrology or flood-return-period MVT feature is encoded
THEN properties include stable segment/network/source/time/value metadata and reject missing or non-finite required values

#### Scenario: Layer metadata discovery
WHEN frontend requests MVT-capable layer metadata
THEN metadata includes `layer_id`, `tile_format`, URL template placeholders, MapLibre source-layer id, property schema/version, min/max zoom, Web Mercator bounds, valid_time/source references, cache etag/version, and fallback/release-blocking flags

