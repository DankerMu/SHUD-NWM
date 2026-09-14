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
- **WHEN** temperature grid, other meteorology grid, or meteorology station data contracts are not implemented
- **THEN** their toggles MUST be disabled or marked unavailable
- **AND** the UI MUST not pretend those layers are rendering
- **AND** the precipitation overlay MUST NOT be listed as unimplemented once the `precip` catalog entry is served

### Requirement: Source and scenario controls drive layer data

The system SHALL support explicit GFS/IFS source selection for the national overview. The basin-detail source/scenario choices (Best Available, GFS + IFS 对比) are retired with the basin-detail lane (#2109 decision B).

#### Scenario: Source selector renders required choices
- **WHEN** the national overview controls render
- **THEN** the operator MUST be able to select GFS or IFS via a segmented control in the bottom control bar, defaulting to GFS
- **AND** Best Available and GFS + IFS 对比 MUST NOT be offered at national scale

#### Scenario: Source changes data requests and URL state
- **WHEN** the operator changes source/scenario
- **THEN** map layers, precipitation overlay, summaries, selected segment forecast data, cycle list, timeline valid times, and comparison availability MUST refresh for the selected source/scenario
- **AND** the URL query MUST preserve the selected source/scenario where shareable
- **AND** a restored URL with `source=best` at national scale MUST resolve to `gfs`

### Requirement: Timeline is driven by valid times

The system SHALL drive time selection from `/api/v1/layers/discharge/cycles` and `/api/v1/layers/{layer_id}/valid-times?source=&cycle=` as the layer-time contract for the national overview. The bottom control bar SHALL contain a cycle (起报时次) selector, the GFS/IFS segmented control, and the timeline, and SHALL default to the newest cycle at lead 0.

Throughout this capability, "lead 0" names the **first entry of the active cycle's advertised `valid_times[]`**, which equals the cycle instant itself in the fully-covered case. When `mvt-tile-contract` clamps a cycle's list to the intersection coverage window (`max(river_valid_time_start)` after the cycle), the first entry is that clamped instant; the normative selection rule stays "the first entry", and the `+0h … +168h` / 57-entry figures below describe the fully-covered production case rather than constraining the clamped one.

The control bar exists only on the national overview; the basin-detail control bar (run-list cycles, run-metadata valid times) is retired with the basin-detail lane (#2109 decision B).

#### Scenario: Active layer has valid times from layer API
- **WHEN** the national overview loads
- **THEN** the system MUST call `/api/v1/layers/discharge/cycles?source=<source>` (after `mapBootstrapLoading` settles, per `overview-data-contracts`) and MUST consume the default cycle's valid times from `metadata.valid_times`; `/api/v1/layers/discharge/valid-times?source=<source>&cycle=<cycle>` is called only when the selected `(source, cycle)` differs from `(default_source, default_cycle)` (per `frontend-mvt-layer-consumption`)
- **AND** the bottom timeline MUST use the returned `valid_times[]` (3-hour stride, lead +0h … +168h inclusive, 57 entries, matching the canonical f000–f168 product range) for ticks, current-time selection, and next/previous actions
- **AND** ticks MUST be labelled with the lead hour (`+0h`, `+3h`, …) and the valid time
- **AND** the current valid time, source, and cycle MUST be included in map and precipitation requests

#### Scenario: Default position is the cycle start
- **WHEN** the URL carries no `validTime`
- **THEN** the selected valid time MUST be the first entry (lead 0) of the active cycle's list

#### Scenario: Cycle selector is fail-closed
- **WHEN** the cycles endpoint returns an empty list (equivalently, the catalog's `discharge` entry carries `default_cycle: null` and `valid_times: []`)
- **THEN** the cycle selector, timeline, and playback MUST render disabled with a notice that no cycle covers every basin
- **AND** no tiles MUST be requested for a partial cycle, and no request MUST be issued with a literal `{cycle}` segment or a client-invented cycle

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
- **THEN** the legend and status notices MUST be offset above the bar so nothing overlaps it
