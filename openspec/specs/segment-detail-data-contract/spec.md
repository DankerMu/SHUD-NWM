# segment-detail-data-contract Specification

## Purpose
TBD - created by archiving change m12-segment-forecast-detail. Update Purpose after archive.
## Requirements
### Requirement: Normalized segment detail view model
The system SHALL normalize segment identity, basin/model metadata, forecast series, lineage, and availability flags before rendering leaf components.

#### Scenario: Existing API composition
WHEN existing APIs provide all required fields
THEN the view model includes currentQ, qUnit, peak forecast, forecast valid time, source provenance, lineage, basinVersionId, riverNetworkVersionId, and geometry budget status

#### Scenario: Non-finite or unavailable numeric values
WHEN forecast, threshold, station, or weather values are missing, non-numeric, NaN, or infinite
THEN the view model omits those values from charts/KPIs and exposes explicit unavailable flags for the UI

#### Scenario: Endpoint decision required
WHEN existing APIs cannot provide a required design field within bounded request count
THEN the implementation records an endpoint decision note before adding an OpenAPI-backed read-only endpoint
