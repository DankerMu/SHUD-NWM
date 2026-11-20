# forecast-migration Specification

## Purpose
What the map page kept from the legacy forecast page when the frontend moved to React: the MapLibre map and the clickable river segment layer. The forecast side panel and its store were replaced by the river curve window specified in `map-feature-popups`.


## Requirements

### Requirement: Map view with MapLibre GL
The system SHALL render an interactive map using react-map-gl + MapLibre GL JS, preserving the existing map style, tile source, and interaction behavior from the legacy index.html.

#### Scenario: Map initialization
- **WHEN** the forecast page loads
- **THEN** the map MUST render with the same center, zoom, and base layer as the legacy implementation

### Requirement: River segment layer
The system SHALL render GeoJSON river segments on the map with click-to-select interaction.

#### Scenario: Segment click triggers forecast load
- **WHEN** the user clicks on a river segment on the map
- **THEN** the river curve window specified by `map-feature-popups` MUST open and load the forecast data for the selected segment
