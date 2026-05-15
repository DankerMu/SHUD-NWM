## ADDED Requirements

### Requirement: Forecast map uses backend river data

The forecast map SHALL use backend river segment/network data for production segment selection.

#### Scenario: Backend river segment drives forecast request

- **WHEN** the frontend loads the forecast map in production mode
- **THEN** it MUST request river segment/network data from the configured API base
- **AND** each rendered feature MUST include `segment_id`, `basin_version_id`, and `river_network_version_id`
- **AND** clicking a feature MUST request forecast series using the clicked backend identifiers
- **AND** the production default MUST NOT depend on hard-coded demo river IDs

#### Scenario: Initial river network loading is bounded

- **WHEN** the backend reports more river features than the bounded initial map window
- **THEN** the frontend MUST NOT drain every river segment page on mount
- **AND** it MUST limit initial river segment requests to the accepted preview window
- **AND** it MUST show or record partial-result metadata that the displayed river network is a preview until viewport, bbox, or tile loading is implemented

### Requirement: Frontend API base applies consistently

All frontend backend requests SHALL honor the configured `VITE_API_BASE_URL`.

#### Scenario: Cross-origin API deployment

- **WHEN** `VITE_API_BASE_URL` is set to an absolute API origin
- **THEN** forecast, flood alert summary/ranking/timeline, flood return-period tile/GeoJSON, monitoring jobs/stages/trends, and operator action requests MUST target that origin
- **AND** tests MUST cover at least one request from each page group

### Requirement: Production RBAC is not user-spoofable

The frontend SHALL NOT expose a production control that lets users grant themselves operator/admin roles.

#### Scenario: Production role source is fixed

- **WHEN** the app runs in production mode without an explicit dev/test role override
- **THEN** the role selector MUST NOT be shown
- **AND** retry/cancel controls MUST be hidden or disabled for a viewer role
- **AND** retry/cancel controls MUST NOT be shown or actionable solely because `VITE_AUTH_ROLE` is configured as an operator/admin role
- **AND** operator action headers MUST only be sent by the explicit dev/test role override path until a trusted production backend auth/session mechanism exists

### Requirement: Flood alert types match the API contract

Flood alert frontend state SHALL be normalized from OpenAPI-generated schemas without contradicting API field names or threshold semantics.

#### Scenario: Timeline and threshold payloads normalize correctly

- **WHEN** flood alert timeline and threshold payloads are returned by the API
- **THEN** the frontend MUST preserve run id, segment id, valid times, return period, warning level, q values, and frequency thresholds
- **AND** local convenience fields MUST be derived from those API fields with tests

### Requirement: Monitoring trends respect source and scenario filters

Monitoring trend charts SHALL use the same source/scenario context as the monitoring jobs view.

#### Scenario: Source and scenario filters are applied

- **WHEN** a user selects a source and scenario filter in monitoring
- **THEN** jobs, stage-duration metrics, and success-rate metrics MUST include those filters in API requests
- **AND** backend metric endpoints MUST exclude unrelated cycles/runs when filters are provided

### Requirement: Production build and SPA deployment are intentional

The frontend production build SHALL be deployable without unexplained bundle warnings or deep-link failures.

#### Scenario: Build and preview deployment checks pass

- **WHEN** frontend build and deployment checks run
- **THEN** large MapLibre/ECharts/vendor chunks MUST be intentionally split or covered by an explicit checked threshold
- **AND** production preview MUST serve deep links such as `/flood-alerts` and `/monitoring` through the SPA fallback
