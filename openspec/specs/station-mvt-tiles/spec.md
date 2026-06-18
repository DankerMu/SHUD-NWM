# station-mvt-tiles Specification

## Purpose
TBD - created by archiving change issue-342-station-mvt-tiles. Update Purpose after archive.
## Requirements
### Requirement: Station MVT Route
The system SHALL expose a canonical backend Mapbox Vector Tile route for meteorological station points scoped by basin version.

#### Scenario: Fetch station tile
- **WHEN** a client requests `GET /api/v1/tiles/met-stations/{basin_version_id}/{z}/{x}/{y}.pbf` with valid XYZ coordinates and live PostGIS MVT enabled
- **THEN** the API returns `application/x-protobuf` bytes for source-layer `met_stations`
- **AND** response headers include `X-Tile-Layer-ID`, `X-Tile-Checksum`, `X-Tile-Cache`, `X-Tile-Cache-Key`, `X-MVT-Schema-Version`, `ETag`, and `Cache-Control`

#### Scenario: Live PostGIS gate
- **WHEN** live PostGIS MVT is not enabled or the backing dialect is not PostGIS
- **THEN** the route fails with `MVT_LIVE_POSTGIS_UNAVAILABLE`
- **AND** it does not return JSON/GeoJSON fallback content as a `.pbf` response

### Requirement: Station MVT Identity And Properties
Station MVT tiles SHALL bind every feature to the requested basin-version identity and SHALL emit stable station properties.

#### Scenario: Station feature properties
- **WHEN** a station point intersects the requested Web Mercator tile
- **THEN** the MVT feature includes stable station properties including `station_id`, `basin_version_id`, `station_name`, `station_role`, and `active_flag`
- **AND** optional model/source fields are included only when backed by station inventory columns or joins

#### Scenario: Basin-version isolation
- **WHEN** a tile is requested for a basin version
- **THEN** source rows are limited to that `basin_version_id`
- **AND** stations from other basin versions are not encoded into the tile

### Requirement: Station MVT Resource And Error Boundaries
Station MVT tiles SHALL reuse the canonical MVT resource budgets, cache identity, and stable error model.

#### Scenario: Budget and property enforcement
- **WHEN** source station rows exceed configured feature/byte/geometry/property limits or required properties are missing
- **THEN** the API fails with the same stable MVT error family used by existing canonical `.pbf` routes

#### Scenario: Cache identity
- **WHEN** the same station tile is requested repeatedly with unchanged station source identity
- **THEN** the cache key and response headers are stable
- **AND** a cache hit returns the cached `.pbf` bytes only when schema, encoder, source, XYZ, and checksum identity match

### Requirement: Existing Station And Tile Contracts Remain Compatible
Adding station MVT SHALL NOT change existing station JSON APIs or existing river/hydro/flood MVT routes.

#### Scenario: Existing APIs unchanged
- **WHEN** existing `/api/v1/met/stations`, station series, river-network, hydro, hydro-national, or flood-return-period route tests run
- **THEN** their existing response envelopes, route paths, error contracts, and MVT source layers remain unchanged

