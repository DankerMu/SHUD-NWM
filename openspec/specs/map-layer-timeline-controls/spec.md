# map-layer-timeline-controls Specification

## Purpose
TBD - created by archiving change m11-overview-basin-drilldown. Update Purpose after archive.
## Requirements
### Requirement: Layer controls cover hydrology, meteorology, and base groups

The system SHALL provide grouped layer controls matching the design while honestly representing unavailable layers.

#### Scenario: Layer groups render
- **WHEN** the overview page loads
- **THEN** controls MUST group layers into hydrology, meteorology, and base layers
- **AND** hydrology controls MUST include river discharge, stage when supported, flood return period, and warning level
- **AND** base controls MUST include basin boundaries and river network when data is available

#### Scenario: Unimplemented meteorology layers are disabled
- **WHEN** precipitation grid, temperature grid, or meteorology station data contracts are not implemented
- **THEN** their toggles MUST be disabled or marked unavailable
- **AND** the UI MUST not pretend those layers are rendering

### Requirement: Source and scenario controls drive layer data

The system SHALL support the GIS design's source/scenario choices for overview and basin detail data selection.

#### Scenario: Source selector renders required choices
- **WHEN** overview or basin detail controls render
- **THEN** the operator MUST be able to select GFS, IFS, GFS + IFS 对比, and Best Available where the workflow supports source selection
- **AND** unsupported choices for the current basin, layer, cycle, or segment MUST be disabled or marked unavailable with a concise reason

#### Scenario: Source changes data requests and URL state
- **WHEN** the operator changes source/scenario
- **THEN** map layers, summaries, selected segment forecast data, timeline valid times, and comparison availability MUST refresh for the selected source/scenario
- **AND** the URL query MUST preserve the selected source/scenario where shareable

#### Scenario: Best Available exposes provenance
- **WHEN** Best Available is selected
- **THEN** the UI MUST show which source/run/cycle was actually used for the visible map or detail data
- **AND** fallback to a different source MUST not occur silently
- **AND** until a backend best-available endpoint supports map/detail surfaces directly, frontend requests to run, pipeline, flood, and forecast APIs MUST use the resolved concrete GFS or IFS source/scenario, or expose Best Available as unavailable when no concrete source can be resolved

#### Scenario: GFS and IFS comparison is available
- **WHEN** GFS + IFS 对比 is selected and both sources have comparable data
- **THEN** segment detail comparison MUST show both series or make comparison data available to the selected segment panel
- **AND** when comparison data is missing, the compare action MUST show an unavailable state rather than a partial unlabeled chart

### Requirement: Basemap switching is available

The system SHALL support terrain, satellite, and vector basemap choices for the M11 overview and basin maps.

#### Scenario: Basemap changes map style
- **WHEN** an operator selects terrain, satellite, or vector basemap
- **THEN** the map MUST switch to the selected basemap
- **AND** active data layers MUST be restored after a successful basemap switch

### Requirement: Legends reflect active hydrologic layer

The system SHALL show legends that match the active hydrologic display variable.

#### Scenario: Discharge legend is active
- **WHEN** the river discharge layer is active
- **THEN** the legend MUST show discharge units and bins suitable for the selected scale or available data

#### Scenario: Return-period legend is active
- **WHEN** the flood return period or warning layer is active
- **THEN** the legend MUST show return-period or warning-level colors consistent with the flood alert page

### Requirement: Timeline is driven by valid times

The system SHALL drive time selection from `/api/v1/layers/{layer_id}/valid-times` as the primary layer-time contract and from payload-derived valid times only for non-layer detail payloads that do not have a layer contract.

#### Scenario: Active layer has valid times from layer API
- **WHEN** an active layer is selected
- **THEN** the system MUST call or consume data from `/api/v1/layers` and `/api/v1/layers/{layer_id}/valid-times`
- **AND** the bottom timeline MUST use the returned `valid_times[]` for ticks, current-time selection, and next/previous actions
- **AND** the current valid time MUST be included in map and adapter requests that support it

#### Scenario: Non-layer detail payload derives valid times
- **WHEN** a selected segment forecast or flood alert timeline is the active detail source and no layer valid-time contract applies
- **THEN** the bottom timeline MUST use those exact times for ticks, current-time selection, and next/previous actions
- **AND** the UI MUST mark the timeline source as derived from the selected payload

#### Scenario: Active layer changes
- **WHEN** an operator switches the active layer
- **THEN** the timeline MUST switch to the new layer's valid-time list
- **AND** if the previous valid time is not valid for the new layer, the system MUST select a documented fallback without rendering stale map data

#### Scenario: No valid times exist
- **WHEN** no valid times are available for the active layer
- **THEN** the timeline MUST show an empty or disabled state
- **AND** playback controls MUST be disabled

#### Scenario: Timeline renders design metadata
- **WHEN** valid-time metadata includes native time resolution, analysis/forecast boundary, or data-source label
- **THEN** the timeline MUST render ticks according to native time resolution
- **AND** it MUST show the current data-source label
- **AND** it MUST show the Analysis/Forecast divider and current-time marker

#### Scenario: Timeline slider is dragged
- **WHEN** an operator drags the timeline slider to an available valid time
- **THEN** the selected valid time MUST update
- **AND** map layers, summaries, and selected segment data that depend on valid time MUST refresh without selecting intermediate invalid times

### Requirement: Playback controls are bounded by valid times

The system SHALL provide previous, play/pause, next, and speed controls that operate only over available valid times.

#### Scenario: Playback advances through valid times
- **WHEN** an operator starts playback
- **THEN** the timeline MUST advance through available valid times in order at the selected speed
- **AND** it MUST stop, loop, or pause at the end according to a documented behavior

#### Scenario: Previous and next respect boundaries
- **WHEN** the current valid time is first or last in the active list
- **THEN** unavailable previous or next actions MUST be disabled or handled without selecting an invalid time

