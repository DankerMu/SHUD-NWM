# frontend-navigation-state Specification

## Purpose
TBD - created by archiving change m11-overview-basin-drilldown. Update Purpose after archive.
## Requirements
### Requirement: Global navigation matches product workflow

The system SHALL update frontend navigation to expose the product workflow defined by the GIS design while preserving implemented pages.

#### Scenario: Navigation contains implemented workflow entries
- **WHEN** the app shell renders
- **THEN** navigation MUST include 全国总览, 水文预报, and 产品监控 entries
- **AND** entries for unimplemented larger pages such as 气象数据 or 系统管理 MUST be either hidden, disabled, or linked to an explicit placeholder according to the implementation decision

#### Scenario: Existing implemented pages remain accessible
- **WHEN** an operator navigates to existing forecast or monitoring workflows
- **THEN** those pages MUST remain reachable after the overview route migration
- **AND** their tests MUST be updated or preserved to assert the new route locations

### Requirement: URL query restores shareable state

The system SHALL encode shareable overview state in URL query parameters. Basin-detail state is retired (#2109 decision B); `basinId` is not a query key.

#### Scenario: Overview query is restored
- **WHEN** an operator opens an overview URL containing valid `source`, `cycle`, `validTime`, `layer`, or `basemap` state
- **THEN** the overview page MUST initialize controls and map data from those parameters

#### Scenario: Invalid query is corrected
- **WHEN** a URL query contains invalid source, layer, basemap, version, segment, or valid-time values
- **THEN** the page MUST fall back to a valid documented default
- **AND** it MUST avoid repeated URL update loops

#### Scenario: Retired basinId key is dropped
- **WHEN** a URL query contains `basinId`
- **THEN** the page MUST replace the URL with the normalised query without `basinId` and render the national overview

### Requirement: Cross-page handoff preserves relevant context

The system SHALL preserve relevant operator context when moving between overview, monitoring, and future detail pages.

#### Scenario: Overview links to display coverage
- **WHEN** an operator clicks the display coverage summary from the overview page
- **THEN** the monitoring route MUST receive available source, cycle, run, or valid-time context through URL query where supported

#### Scenario: Overview links to monitoring
- **WHEN** an operator clicks the forecast run summary from the overview page
- **THEN** the monitoring route MUST receive available source/cycle context through URL query where supported

### Requirement: Frontend validation covers the route and state contract

The system SHALL include automated tests that prevent route/state regressions.

#### Scenario: Unit and component route tests run
- **WHEN** frontend unit tests run
- **THEN** they MUST cover route definitions, navigation labels, query parsing, query serialization, and invalid-query fallback behavior
- **AND** they MUST cover that the legacy `/basins/:basinId` redirect and a `?basinId=` query both land on the national overview with no `basinId` key in the final URL (#2109 decision B)

#### Scenario: Playwright route smoke tests run
- **WHEN** frontend end-to-end tests run
- **THEN** they MUST cover `/`, `/overview`, `/forecast` with mocked or fixture data, and existing implemented routes

