# station-mvt-tiles Specification

## Purpose
Record the station vector-tile surface after its retirement: the `/api/v1/tiles/met-stations/...` route is gone (2026-11-06, #2773 item 4), while the station list API and the other tile contracts it sat beside remain compatible.

## Requirements

### Requirement: Existing Station And Tile Contracts Remain Compatible

Retiring station MVT SHALL NOT change existing station JSON APIs or existing river/hydro MVT routes.

#### Scenario: Existing APIs unchanged

- **WHEN** existing `/api/v1/met/stations`, station series, river-network, hydro, or hydro-national route tests run
- **THEN** their existing response envelopes, route paths, error contracts, and MVT source layers remain unchanged

### Requirement: Station MVT Route Is Retired

The system SHALL NOT expose a backend Mapbox Vector Tile route for meteorological station points.
`GET /api/v1/tiles/met-stations/{basin_version_id}/{z}/{x}/{y}.pbf` SHALL NOT be registered on the API router and SHALL NOT be published in the static or
runtime OpenAPI contract. Station points reach the map only through `GET /api/v1/met/stations`.

#### Scenario: Retired station tile URL is an unknown path

- **WHEN** a client requests `GET /api/v1/tiles/met-stations/{basin_version_id}/{z}/{x}/{y}.pbf`
- **THEN** the app router answers HTTP 404 for an unknown path, without running a tile handler or reading `met.met_station`
- **AND** the body is not the `MVT_SOURCE_IDENTITY_NOT_FOUND` tile error envelope

#### Scenario: Retired station tile route is absent from the API contract

- **WHEN** the static `openapi/nhms.v1.yaml` and the runtime OpenAPI document are read
- **THEN** neither contains the path `/api/v1/tiles/met-stations/{basin_version_id}/{z}/{x}/{y}.pbf` nor the operation `getMetStationTile`
- **AND** both still publish `/api/v1/met/stations`
