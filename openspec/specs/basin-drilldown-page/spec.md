# basin-drilldown-page Specification

## Purpose
TBD - created by archiving change m11-overview-basin-drilldown. Update Purpose after archive.
## Requirements
### Requirement: Basin drill-down route renders basin-scoped analysis

The system SHALL provide a basin detail route that focuses the map and side panels on one basin and its river segments.

#### Scenario: Enter from overview basin popup
- **WHEN** an operator clicks "进入分析" from an overview basin popup
- **THEN** the application MUST navigate to `/basins/:basinId`
- **AND** the map MUST fly or fit to the basin bbox when available
- **AND** the page MUST select an active basin version using URL state, backend active version metadata, or a documented fallback

#### Scenario: Enter from hydrologic forecast navigation
- **WHEN** an operator opens 水文预报 and selects a basin
- **THEN** the application MUST open the same basin drill-down capability
- **AND** it MUST not create a separate incompatible basin detail workflow

### Requirement: Basin detail left panel supports segment discovery

The system SHALL show basin identity, selected basin version, and a searchable/filterable river segment list.

#### Scenario: Segment list loads for basin version
- **WHEN** a basin version has river segments
- **THEN** the left panel MUST show basin name and `basin_version_id`
- **AND** each segment row MUST show segment name or ID, current Q when available, and return-period or warning-level color when available

#### Scenario: Search and warning filter segment list
- **WHEN** an operator enters a search term or selects a warning-level filter
- **THEN** the segment list MUST update without reloading the whole page
- **AND** the result count or empty state MUST reflect the applied filters

#### Scenario: Basin has no river segments
- **WHEN** the selected basin version has no river segment data
- **THEN** the page MUST show the documented empty state "该流域暂无已发布的预报数据" or an equivalent concise message
- **AND** map/list controls that require segments MUST be disabled

#### Scenario: Invalid basin id
- **WHEN** an operator opens `/basins/:basinId` for a basin that does not exist
- **THEN** the page MUST show a scoped not-found state
- **AND** it MUST provide a route back to 全国总览

#### Scenario: Basin bbox is missing
- **WHEN** basin detail data loads without bbox geometry
- **THEN** the map MUST use a documented fallback extent
- **AND** the page MUST show a scoped missing-geometry note without blocking the segment list

### Requirement: Basin map supports segment hover and click

The system SHALL render basin-scoped river segments and support hover/click interactions.

#### Scenario: Hover highlights river segment
- **WHEN** an operator hovers a river segment on the basin map
- **THEN** the segment MUST be visually highlighted
- **AND** a tooltip MUST show segment name or ID, current flow when available, and return period or warning level when available

#### Scenario: Click selects river segment
- **WHEN** an operator clicks a river segment
- **THEN** the selected segment ID MUST be reflected in state and URL query
- **AND** the corresponding segment row MUST be selected or scrolled into view when present
- **AND** the selected segment detail panel MUST load

#### Scenario: Segment row selects map segment
- **WHEN** an operator clicks a segment row in the left panel
- **THEN** the map MUST highlight the same segment
- **AND** the URL query MUST include the selected `segmentId`
- **AND** the detail panel MUST load for that segment

#### Scenario: Basin context layers render
- **WHEN** basin boundary, city labels, or station labels are available
- **THEN** the basin map MUST highlight the basin boundary and render available labels according to the design
- **AND** absent city/station labels MUST not block the river network or segment interactions

### Requirement: Selected segment detail provides forecast context

The system SHALL show selected segment metadata, current forecast values, source/cycle information, quality status, trend preview, and handoff actions.

#### Scenario: Segment detail renders available fields
- **WHEN** selected segment detail and forecast data are available
- **THEN** the detail panel MUST show `river_segment_id`, basin name, model identifier when available, catchment area or length when available, current Q, water-level delta when available, return-period level, forecast valid time, source, and cycle time

#### Scenario: Trend sparkline is shown for selected segment
- **WHEN** recent or forecast trend points are available for the selected segment
- **THEN** the right panel MUST show a compact sparkline for the segment
- **AND** it MUST mark the current value and trend direction when those values can be derived

#### Scenario: Segment handoff actions are explicit
- **WHEN** a segment is selected
- **THEN** the detail panel MUST expose "查看详情" as a handoff to the future full-screen forecast detail route or an implemented existing detail route
- **AND** it MUST expose "对比预报" that overlays or requests comparison data when IFS/GFS comparison data exists, otherwise shows a disabled unavailable state

### Requirement: Basin detail includes lineage and quality context

The system SHALL expose enough lineage and quality context to explain stale, missing, or blocked segment data.

#### Scenario: Lineage data is available
- **WHEN** `/api/v1/lineage/river-point` or equivalent lineage data succeeds for the selected context
- **THEN** the segment detail MUST show source/run/QC status or a link to inspect it

#### Scenario: Lineage or QC data is unavailable
- **WHEN** lineage or quality data fails or is unavailable
- **THEN** the segment detail MUST show a scoped unavailable state
- **AND** it MUST not block the map, list, or available forecast values from rendering

### Requirement: Basin detail handles unavailable source/time data

The system SHALL distinguish unavailable forecast, warning, or valid-time data from empty river geometry.

#### Scenario: Selected source/cycle/valid time has no segment data
- **WHEN** the selected source, cycle, or valid time has no forecast or warning data for the basin
- **THEN** the map/list/detail view models MUST expose an unavailable reason
- **AND** the UI MUST keep basin geometry visible while disabling or clearing data-dependent values

