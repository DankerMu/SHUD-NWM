## ADDED Requirements

### Requirement: Map view with MapLibre GL
The system SHALL render an interactive map using react-map-gl + MapLibre GL JS, preserving the existing map style, tile source, and interaction behavior from the legacy index.html.

#### Scenario: Map initialization
- **WHEN** the forecast page loads
- **THEN** the map MUST render with the same center, zoom, and base layer as the legacy implementation

### Requirement: River segment layer
The system SHALL render GeoJSON river segments on the map with click-to-select interaction.

#### Scenario: Segment click triggers forecast load
- **WHEN** the user clicks on a river segment on the map
- **THEN** the forecast panel MUST load and display the forecast data for the selected segment

### Requirement: Forecast panel
The system SHALL display a side panel with: segment name/info, forecast chart (ECharts line chart via echarts-for-react), data source attribution, and error/loading states.

#### Scenario: Forecast chart rendering
- **WHEN** forecast data is loaded for a segment
- **THEN** the chart MUST render the forecast time series with the same axes, labels, and styling as the legacy implementation

#### Scenario: Forecast load error
- **WHEN** the forecast API call fails
- **THEN** the panel MUST display an error message with a retry button

### Requirement: Forecast state management
The system SHALL manage forecast state via Zustand store: selected segment, forecast data, loading/error state.

#### Scenario: State reset on new segment
- **WHEN** the user clicks a different river segment
- **THEN** the previous forecast data MUST be cleared and a new fetch MUST begin
