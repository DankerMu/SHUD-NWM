# flood-tile-delivery-contract Specification

## Purpose
TBD - created by archiving change m7-second-review-remediation. Update Purpose after archive.
## Requirements
### Requirement: Flood tile backend and frontend format agreement
Flood return-period map tile delivery SHALL use one consistent payload format across backend, OpenAPI, and frontend.

#### Scenario: MVT delivery is selected
- **WHEN** the flood tile endpoint path ends in `.pbf`
- **THEN** the backend MUST return decodable Mapbox Vector Tile bytes with `application/x-protobuf`
- **AND** OpenAPI MUST document binary protobuf content
- **AND** the frontend MUST load it as a MapLibre vector source with the documented source layer

#### Scenario: GeoJSON delivery is selected
- **WHEN** the backend returns JSON feature collections for flood return-period map data
- **THEN** the endpoint path, content type, OpenAPI schema, and frontend source type MUST all describe GeoJSON rather than `.pbf` vector tiles
- **AND** any existing `.pbf` path MUST have an explicit compatibility behavior such as documented deprecation, redirect, or supported legacy response

### Requirement: Flood tile feature properties
Flood return-period map payloads SHALL expose the properties required by the tile publication and frontend contracts.

#### Scenario: Flood tile properties are emitted
- **WHEN** a flood return-period map payload is returned
- **THEN** each feature MUST include a stable segment identifier, displayed value, unit, quality flag, return period, and warning level
- **AND** field names MUST be documented consistently across OpenAPI, frontend layer code, and tile module docs

### Requirement: Flood tile spatial semantics
Flood tile endpoints SHALL honor tile coordinate semantics or explicitly avoid tile URLs.

#### Scenario: Tile URL includes z/x/y
- **WHEN** a request includes `{z}/{x}/{y}`
- **THEN** the backend MUST use those coordinates to constrain, simplify, or encode the returned map payload for that tile
- **AND** national-scale requests MUST NOT return the full return-period result set for every tile

#### Scenario: Tile is requested before frequency results are ready
- **WHEN** the target run is not frequency-ready
- **THEN** the endpoint MUST return the documented error envelope
- **AND** the frontend MUST handle the error without rendering a broken map layer

### Requirement: Flood tile contract tests
Flood tile behavior SHALL be covered by tests that validate content type, payload decode, and frontend source expectations.

#### Scenario: Backend format regression
- **WHEN** the flood tile route is exercised in tests
- **THEN** the test MUST assert content type and payload structure or decodability for the selected format

#### Scenario: Frontend source regression
- **WHEN** frontend tests render the flood alert map layer
- **THEN** they MUST assert that the configured MapLibre source type matches the documented backend format

