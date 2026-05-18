## ADDED Requirements

### Requirement: Overview pages consume typed view models

The system SHALL isolate national overview and basin drill-down pages from raw API response shapes through typed adapters or stores.

#### Scenario: Raw API data is normalized before page rendering
- **WHEN** overview or basin page components render basin, summary, segment, warning, forecast, or lineage data
- **THEN** those components MUST consume typed frontend view models
- **AND** normalization of nullable fields, units, warning levels, quality flags, timestamps, and display names MUST occur in adapters/stores rather than in leaf UI components

#### Scenario: Adapter tests cover required view models
- **WHEN** frontend tests run
- **THEN** they MUST cover normalization for overview basins, overview summaries, layer state, basin detail, segment rows, and selected segment detail

### Requirement: Existing API contracts are reused first

The system SHALL compose current backend APIs before adding new aggregation endpoints.

#### Scenario: Overview data loads from existing endpoints
- **WHEN** the overview page fetches data
- **THEN** it MUST use existing basins, model asset, flood alert, pipeline, tile, and river segment APIs where sufficient
- **AND** new endpoints MUST NOT be added solely for convenience if frontend adapters can satisfy the requirement within acceptable complexity

#### Scenario: Aggregation endpoint is justified
- **WHEN** an implementation adds `GET /api/v1/overview/summary`, `GET /api/v1/basins/{basin_id}/summary`, or another M11 aggregation endpoint
- **THEN** the PR MUST include OpenAPI updates, generated frontend types, backend route/schema tests, and frontend adapter tests
- **AND** the endpoint MUST be read-only and scoped to fields required by the M11 pages

### Requirement: ID and version fields remain explicit

The system SHALL preserve domain IDs and version identifiers across view models, routes, and handoff links.

#### Scenario: Basin version is selected
- **WHEN** a basin detail page chooses a basin version
- **THEN** the selected `basin_version_id` MUST be visible in state and in the UI where the design calls for it
- **AND** segment API calls MUST use the selected basin version rather than an implicit global version

#### Scenario: Segment ID is selected
- **WHEN** a segment is selected from the map, list, or URL query
- **THEN** the same `river_segment_id` or API-required segment identifier MUST be used consistently for detail, forecast series, flood alert timeline, lineage, and handoff links

### Requirement: Data freshness and unavailable states are represented

The system SHALL distinguish current data, stale data, unavailable data, and partial failures in the view models.

#### Scenario: Latest update is available
- **WHEN** a summary or layer payload includes latest update, cycle, run, or valid-time metadata
- **THEN** the view model MUST expose that freshness metadata to the summary panel or timeline

#### Scenario: Data is unavailable
- **WHEN** a required field or endpoint is unavailable
- **THEN** the view model MUST expose an unavailable reason or quality note
- **AND** UI components MUST show a scoped empty/disabled/error state instead of fabricating values
