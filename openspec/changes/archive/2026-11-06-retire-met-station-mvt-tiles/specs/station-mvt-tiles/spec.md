## ADDED Requirements

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

## MODIFIED Requirements

### Requirement: Existing Station And Tile Contracts Remain Compatible

Retiring station MVT SHALL NOT change existing station JSON APIs or existing river/hydro MVT routes.

#### Scenario: Existing APIs unchanged

- **WHEN** existing `/api/v1/met/stations`, station series, river-network, hydro, or hydro-national route tests run
- **THEN** their existing response envelopes, route paths, error contracts, and MVT source layers remain unchanged

## REMOVED Requirements

### Requirement: Station MVT Route

**Reason**: The route listed `met.met_station` rows with `active_flag = true`. Every Direct Grid station is `active_flag = false`, so on node-27 it answered 404
`MVT_SOURCE_IDENTITY_NOT_FOUND` for every Direct Grid basin. The frontend never requested it and the display API journal showed 0 requests in 7 days. Owner
decision on #2773 item 4 (2026-11-06).
**Migration**: None needed. Station points are served by `GET /api/v1/met/stations`, which is unchanged.

### Requirement: Station MVT Identity And Properties

**Reason**: Retired together with the station MVT route (#2773 item 4); there is no station tile left to carry identity or properties.

**Migration**: None needed. `/api/v1/met/stations` items keep their station identity fields.

### Requirement: Station MVT Resource And Error Boundaries

**Reason**: Retired together with the station MVT route (#2773 item 4); the station tile budgets, cache identity (`_station_source_version`) and error mapping have no route to serve.

**Migration**: None needed. The canonical MVT budgets and error model stay in force for the river-network, hydro and hydro-national routes.
