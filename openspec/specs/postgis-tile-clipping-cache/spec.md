# postgis-tile-clipping-cache Specification

## Purpose
TBD - created by archiving change m16-production-mvt-performance. Update Purpose after archive.
## Requirements
### Requirement: PostGIS clipping and cache
The tile service SHALL clip/simplify in PostGIS, respect feature/coordinate budgets, and maintain `map.tile_layer`/`map.tile_cache` metadata.

#### Scenario: SQL shape
WHEN building a production tile query
THEN the query uses tile-envelope clipping/simplification and MVT geometry/encoding primitives instead of materializing a full national GeoJSON FeatureCollection in application memory

#### Scenario: Web Mercator tile matrix
WHEN encoding z/x/y MVT
THEN tile bounds use standard Web Mercator XYZ semantics, source geometries are transformed before encoding, extent/buffer/simplification are documented, and encoded coordinates remain inside the allowed MVT extent plus buffer

#### Scenario: Cache hit
WHEN tile has valid cached checksum/etag
THEN service serves or records the cached tile without duplicate cache rows

#### Scenario: Cache identity
WHEN layer, run/source/version, valid_time, z/x/y, style-affecting parameters, or encoder/schema version changes
THEN cache identity changes or the prior cache entry is invalidated before serving

#### Scenario: Oversized tile
WHEN tile exceeds configured feature/coordinate/payload budget
THEN service returns bounded degraded/error response and records evidence

#### Scenario: Real PostGIS opt-in
WHEN real PostGIS credentials are absent
THEN live SQL proof is recorded as not_executed/release_blocked without failing deterministic CI or claiming readiness

