## MODIFIED Requirements

### Requirement: Layer controls cover hydrology, meteorology, and base groups

The system SHALL provide grouped layer controls matching the design while honestly representing unavailable layers.

#### Scenario: Layer groups render
- **WHEN** the overview page loads
- **THEN** controls MUST group layers into hydrology, meteorology, and base layers
- **AND** hydrology controls MUST include river discharge and stage when supported
- **AND** meteorology controls MUST include the past-24h precipitation overlay toggle (default on) and the 气象代站 toggle
- **AND** base controls MUST include basin boundaries and river network when data is available

#### Scenario: Unimplemented meteorology layers are disabled
- **WHEN** temperature grid or other meteorology grid contracts are not implemented
- **THEN** their toggles MUST be disabled or marked unavailable
- **AND** the UI MUST not pretend those layers are rendering
- **AND** the precipitation overlay MUST NOT be listed as unimplemented once the `precip` catalog entry is served

### Requirement: Source and scenario controls drive layer data

The system SHALL support explicit GFS/IFS source selection for the national overview and the existing source/scenario choices for basin detail data selection.

#### Scenario: Source selector renders required choices
- **WHEN** the national overview controls render
- **THEN** the operator MUST be able to select GFS or IFS via a segmented control in the bottom control bar, defaulting to GFS
- **AND** Best Available and GFS + IFS 对比 MUST NOT be offered at national scale
- **WHEN** basin detail controls render
- **THEN** the operator MUST be able to select GFS, IFS, GFS + IFS 对比, and Best Available where the workflow supports source selection
- **AND** unsupported choices for the current basin, layer, cycle, or segment MUST be disabled or marked unavailable with a concise reason

#### Scenario: Source changes data requests and URL state
- **WHEN** the operator changes source/scenario
- **THEN** map layers, precipitation overlay, summaries, selected segment forecast data, cycle list, timeline valid times, and comparison availability MUST refresh for the selected source/scenario
- **AND** the URL query MUST preserve the selected source/scenario where shareable
- **AND** a restored URL with `source=best` at national scale MUST resolve to `gfs`

#### Scenario: Best Available exposes provenance
- **WHEN** Best Available is selected in basin detail
- **THEN** the UI MUST show which source/run/cycle was actually used for the visible detail data
- **AND** fallback to a different source MUST not occur silently
- **AND** until a backend best-available endpoint supports map/detail surfaces directly, frontend requests to run, pipeline, and forecast APIs MUST use the resolved concrete GFS or IFS source/scenario, or expose Best Available as unavailable when no concrete source can be resolved

#### Scenario: GFS and IFS comparison is available
- **WHEN** GFS + IFS 对比 is selected in basin detail and both sources have comparable data
- **THEN** segment detail comparison MUST show both series or make comparison data available to the selected segment panel
- **AND** when comparison data is missing, the compare action MUST show an unavailable state rather than a partial unlabeled chart

### Requirement: Timeline is driven by valid times

The system SHALL drive time selection from `/api/v1/layers/discharge/cycles` and `/api/v1/layers/{layer_id}/valid-times?source=&cycle=` as the primary layer-time contract for the national overview, and from payload-derived valid times only for non-layer detail payloads that do not have a layer contract. The bottom control bar SHALL contain a cycle (起报时次) selector, the GFS/IFS segmented control, and the timeline, and SHALL default to the newest cycle at lead 0.

#### Scenario: Active layer has valid times from layer API
- **WHEN** the national overview loads
- **THEN** the system MUST call `/api/v1/layers/discharge/cycles?source=<source>` and, for the selected cycle, `/api/v1/layers/discharge/valid-times?source=<source>&cycle=<cycle>`
- **AND** the bottom timeline MUST use the returned `valid_times[]` (3-hour stride, lead 0–167h) for ticks, current-time selection, and next/previous actions
- **AND** ticks MUST be labelled with the lead hour (`+0h`, `+3h`, …) and the valid time
- **AND** the current valid time, source, and cycle MUST be included in map and precipitation requests

#### Scenario: Default position is the cycle start
- **WHEN** the URL carries no `validTime`
- **THEN** the selected valid time MUST be the first entry (lead 0) of the active cycle's list

#### Scenario: Cycle selector is fail-closed
- **WHEN** the cycles endpoint returns an empty list
- **THEN** the cycle selector, timeline, and playback MUST render disabled with a notice that no cycle covers every basin
- **AND** no tiles MUST be requested for a partial cycle

#### Scenario: Non-layer detail payload derives valid times
- **WHEN** a selected segment forecast is the active detail source and no layer valid-time contract applies
- **THEN** the bottom timeline MUST use those exact times for ticks, current-time selection, and next/previous actions
- **AND** the UI MUST mark the timeline source as derived from the selected payload

#### Scenario: Active layer changes
- **WHEN** an operator switches the active layer, source, or cycle
- **THEN** the timeline MUST switch to the new valid-time list
- **AND** if the previous valid time is not valid for the new list, the system MUST select the first entry (lead 0) without rendering stale map data

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
- **AND** map layers, precipitation overlay, summaries, and selected segment data that depend on valid time MUST refresh without selecting intermediate invalid times

#### Scenario: Floating controls clear the control bar
- **WHEN** the bottom control bar is mounted
- **THEN** the legend, back button, and status notices MUST be offset above the bar so nothing overlaps it
