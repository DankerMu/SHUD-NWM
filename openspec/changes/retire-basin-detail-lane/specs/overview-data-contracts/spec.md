## MODIFIED Requirements

### Requirement: Overview pages consume typed view models

The system SHALL isolate the national overview page from raw API response shapes through typed adapters or stores.

#### Scenario: Raw API data is normalized before page rendering
- **WHEN** overview page components or overview popups render basin, summary, segment, or forecast data
- **THEN** those components MUST consume typed frontend view models
- **AND** normalization of nullable fields, units, quality flags, timestamps, and display names MUST occur in adapters/stores rather than in leaf UI components

#### Scenario: Adapter tests cover required view models
- **WHEN** frontend tests run
- **THEN** they MUST cover normalization for overview basins, overview summaries, and layer state

### Requirement: ID and version fields remain explicit

The system SHALL preserve domain IDs and version identifiers across view models, routes, and handoff links.

#### Scenario: Segment ID is selected
- **WHEN** a segment is selected from the map
- **THEN** the same `river_segment_id` or API-required segment identifier MUST be used consistently for the forecast popup's series requests

### Requirement: Data freshness and unavailable states are represented

The system SHALL distinguish current data, stale data, unavailable data, and partial failures in the view models.

#### Scenario: Latest update is available
- **WHEN** a summary or layer payload includes latest update, cycle, run, or valid-time metadata
- **THEN** the view model MUST expose that freshness metadata to the summary panel or timeline

#### Scenario: Data is unavailable
- **WHEN** a required field or endpoint is unavailable
- **THEN** the view model MUST expose an unavailable reason or quality note
- **AND** UI components MUST show a scoped empty/disabled/error state instead of fabricating values

#### Scenario: Compare detail surfaces need aggregation
- **WHEN** an overview query requests `source=compare`
- **THEN** selected-segment comparison surfaces MUST NOT be populated from a single run
- **AND** until a GFS+IFS aggregation/composition endpoint exists, those surfaces MUST expose a scoped unavailable or aggregation-needed state while source availability may still reflect the run set

