# Capability Spec: forecast-frontend

## Context

The NHMS M1 frontend provides the minimal interactive interface for viewing forecast results: a MapLibre GL JS river network basemap with segment interaction and ECharts forecast flow charts. In M1 scope, river segments are loaded as GeoJSON LineString features (vector tiles deferred to M3). The frontend calls `GET /api/v1/basin-versions/{basin_version_id}/river-segments/{segment_id}/forecast-series` to fetch 7-day forecast data. The frontend MUST know the active `basin_version_id` to construct API calls. UI design tokens follow `docs/spec/06B_frontend_ui_design_spec.md`. Layout is responsive with Chinese labels.

---

## ADDED Requirements

### Requirement: Map initialization

The frontend SHALL initialize a MapLibre GL JS map instance with a basemap suitable for hydrological visualization. The map MUST be responsive and fill the available viewport.

#### Scenario: Map renders on page load

- **WHEN** the user navigates to the forecast page
- **THEN** a MapLibre GL JS map MUST render within the main content area
- **THEN** the map MUST use a basemap style that supports terrain/water visualization
- **THEN** the map MUST be centered on the demo basin (Changjiang) with an appropriate zoom level to show all river segments

#### Scenario: Map is responsive to viewport changes

- **WHEN** the browser window is resized
- **THEN** the map MUST resize to fill the available viewport width and height
- **THEN** no horizontal scrollbar MUST appear
- **THEN** map controls (zoom, attribution) MUST remain visible and accessible

#### Scenario: Map controls are available

- **WHEN** the map is initialized
- **THEN** zoom in/out controls MUST be displayed
- **THEN** a scale bar MUST be visible
- **THEN** attribution text MUST be present per MapLibre requirements

---

### Requirement: River network layer

The frontend SHALL render river segments as a GeoJSON LineString layer on the map. Each segment MUST be visually distinguishable and styled according to the design spec.

#### Scenario: River segments are rendered from GeoJSON

- **WHEN** the map finishes initialization
- **THEN** the river network layer MUST be loaded from a GeoJSON source containing LineString features
- **THEN** each feature MUST include properties: `segment_id` (e.g., `yangtze_v12_riv_000001`), `name`, `stream_order`, `basin_version_id`, and `river_network_version_id`
- **THEN** all segments in the demo basin MUST be visible on the map

#### Scenario: Segments are styled by stream order

- **WHEN** river segments are rendered
- **THEN** line width MUST vary by `stream_order` (higher order = wider line)
- **THEN** line color MUST use the river/water color defined in the design tokens
- **THEN** segments MUST be rendered above the basemap but below any overlay UI elements

#### Scenario: GeoJSON source is used in M1

- **WHEN** the frontend loads river network data in M1
- **THEN** it MUST use a static or API-served GeoJSON file (not vector tiles)
- **THEN** the GeoJSON MUST contain fewer than 100 features for the demo basin

---

### Requirement: Segment hover interaction

The frontend SHALL provide visual feedback when the user hovers over a river segment, including highlight styling and a tooltip with segment identification.

#### Scenario: Segment highlights on hover

- **WHEN** the user moves the mouse cursor over a river segment on the map
- **THEN** the hovered segment MUST change to a highlight color (distinct from the default river color)
- **THEN** the line width of the hovered segment MUST increase visually
- **THEN** the cursor MUST change to a pointer style

#### Scenario: Tooltip displays segment info on hover

- **WHEN** the user hovers over a river segment
- **THEN** a tooltip MUST appear near the cursor position
- **THEN** the tooltip MUST display the `segment_id` and `name` of the segment in Chinese labels
- **THEN** the tooltip MUST disappear when the cursor leaves the segment

#### Scenario: Only one segment is highlighted at a time

- **WHEN** the user moves the cursor from one segment to another
- **THEN** the previously highlighted segment MUST return to its default style
- **THEN** the new segment MUST receive the highlight style
- **THEN** there MUST NOT be multiple segments highlighted simultaneously

---

### Requirement: Segment click interaction

The frontend SHALL open a popup or side panel when the user clicks a river segment. The panel MUST display the forecast chart for the selected segment.

#### Scenario: Click opens forecast panel

- **WHEN** the user clicks on a river segment on the map
- **THEN** a popup or side panel MUST open
- **THEN** the panel MUST display the segment name as the title
- **THEN** the panel MUST contain a loading indicator while forecast data is being fetched

#### Scenario: Panel closes on dismiss action

- **WHEN** a forecast panel is open and the user clicks the close button or clicks on empty map area
- **THEN** the panel MUST close
- **THEN** the selected segment MUST return to its default (non-highlighted) style

#### Scenario: Clicking a different segment switches the panel

- **WHEN** a forecast panel is open for segment A and the user clicks on segment B
- **THEN** the panel MUST update to show segment B's name and forecast data
- **THEN** a new API request MUST be made for segment B's forecast series

---

### Requirement: Forecast chart display

The frontend SHALL render a 7-day forecast flow chart using ECharts within the segment detail panel. The chart displays flow rate (m3/s) against forecast timestamps with appropriate Chinese labels.

#### Scenario: Chart renders 7-day forecast curve

- **WHEN** forecast data is successfully loaded for a segment
- **THEN** an ECharts line chart MUST be rendered in the panel
- **THEN** the x-axis MUST represent the timestamp (first element of each `[timestamp, value]` tuple) spanning approximately 7 days
- **THEN** the y-axis MUST represent flow rate in m3/s with the label "流量 (m³/s)"
- **THEN** the chart title MUST include the segment name and the `issue_time` of the forecast

#### Scenario: Chart axes are properly formatted

- **WHEN** the chart is displayed
- **THEN** x-axis labels MUST show date and time in a human-readable format (e.g., "05-07 00:00")
- **THEN** y-axis MUST use auto-scaling with reasonable tick intervals
- **THEN** the chart MUST include a tooltip that shows exact timestamp and value on hover

#### Scenario: Chart uses design tokens for styling

- **WHEN** the chart is rendered
- **THEN** colors MUST follow the design tokens from `docs/spec/06B_frontend_ui_design_spec.md`
- **THEN** the chart line color MUST use the primary accent color
- **THEN** font family and sizes MUST match the global design token definitions

---

### Requirement: Chart-API integration

The frontend SHALL fetch forecast data from `GET /api/v1/basin-versions/{basin_version_id}/river-segments/{segment_id}/forecast-series` when a segment is clicked. The frontend MUST know the active `basin_version_id` (from the GeoJSON feature properties or application state) to construct the correct API path. The response MUST be correctly parsed and mapped to the ECharts data model.

#### Scenario: API is called on segment click

- **WHEN** the user clicks a river segment with `segment_id = "yangtze_v12_riv_000001"` and the active `basin_version_id = "yangtze_v12"`
- **THEN** the frontend MUST send `GET /api/v1/basin-versions/yangtze_v12/river-segments/yangtze_v12_riv_000001/forecast-series?issue_time=latest&variables=q_down`
- **THEN** the request MUST include appropriate headers (`Accept: application/json`)

#### Scenario: API response is mapped to chart data

- **WHEN** the API returns a successful response with `series[0].points`
- **THEN** each point MUST be parsed as a `[timestamp, value]` tuple (two-element array)
- **THEN** the first element (timestamp, ISO 8601) MUST be mapped to the x-axis
- **THEN** the second element (value, float) MUST be mapped to the y-axis
- **THEN** the chart `unit` MUST be read from the response `unit` field

#### Scenario: API error is handled gracefully

- **WHEN** the API returns an error response (4xx or 5xx)
- **THEN** the chart area MUST display an error message in Chinese (e.g., "数据加载失败，请稍后重试")
- **THEN** the error message MUST include the `request_id` from the error response for troubleshooting
- **THEN** the panel MUST remain open (not close on error)

---

### Requirement: Loading and error states

The frontend SHALL provide clear visual feedback for loading, empty, and error states across all data-driven components. States MUST use Chinese language labels.

#### Scenario: Loading state is shown during data fetch

- **WHEN** the frontend is waiting for an API response (forecast-series or GeoJSON)
- **THEN** a loading spinner or skeleton placeholder MUST be displayed
- **THEN** the loading indicator MUST include a Chinese label (e.g., "加载中...")
- **THEN** user interaction with the loading component MUST be disabled (no duplicate requests)

#### Scenario: Empty state is shown when no data exists

- **WHEN** the API returns HTTP 200 with an empty `series` array (no forecast data available)
- **THEN** the chart area MUST display a message: "暂无预报数据"
- **THEN** the panel MUST remain open and the segment name MUST still be visible
- **THEN** the empty state MUST be visually distinct from the loading and error states

#### Scenario: Network error shows retry option

- **WHEN** the API request fails due to a network error (timeout, connection refused)
- **THEN** an error message MUST be displayed: "网络连接失败"
- **THEN** a retry button MUST be provided to re-attempt the API call
- **THEN** clicking retry MUST re-send the same API request and show the loading state

#### Scenario: Map layer loading failure shows fallback

- **WHEN** the GeoJSON river network source fails to load
- **THEN** the map MUST still render the basemap without the river layer
- **THEN** an error banner MUST appear at the top of the page indicating "河网数据加载失败"
- **THEN** the error MUST NOT prevent the map from being interactive (zoom, pan)
