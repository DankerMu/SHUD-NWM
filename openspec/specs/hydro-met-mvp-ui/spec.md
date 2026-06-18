# hydro-met-mvp-ui Specification

## Purpose
TBD - created by archiving change m21-qhh-hydro-met-ops-mvp. Update Purpose after archive.
## Requirements
### Requirement: Hydro-met MVP entry

The frontend SHALL expose a two-purpose hydrology/meteorology MVP entry for QHH or limited basins.

#### Scenario: Navigation entry
- **WHEN** the app shell renders for the MVP build
- **THEN** the visible workflow includes a hydrology/meteorology entry at `/hydro-met` or an equivalent route alias
- **AND** existing routes remain accessible for regression and deep-link compatibility.

#### Scenario: Latest product bootstrap
- **WHEN** `/hydro-met` loads without explicit IDs
- **THEN** it requests the latest QHH display product for the selected source
- **AND** it uses returned identifiers to load station inventory, river segment candidates, and chart data.

#### Scenario: MVP scope language
- **WHEN** the page labels hydrologic data
- **THEN** it uses river discharge or river-segment flow language for `q_down`
- **AND** it does not label `q_down` as water level or stage.

### Requirement: Station and river chart interactions

The hydro-met MVP UI SHALL show real station forcing curves and river-segment `q_down` curves for selected items.

#### Scenario: Station selection
- **WHEN** a user selects a station row or map marker
- **THEN** the right-side chart area requests the station series API for `PRCP`, `TEMP`, `RH`, `wind`, `Rn`, and `Press`
- **AND** charts show values, units, source, cycle, forcing version, valid-time range, and quality flags from the API.

#### Scenario: River segment selection
- **WHEN** a user selects a river segment row or map feature
- **THEN** the chart area requests forecast-series for that basin version, river network version, segment, selected source/scenario, and variable `q_down`
- **AND** it displays nonempty real series with unit metadata when available.

#### Scenario: Empty or unavailable data
- **WHEN** station series or river forecast data is absent, invalid, restricted, or unavailable
- **THEN** the page renders an explicit unavailable/empty/error state
- **AND** it does not draw fake curves or silently switch to another station or segment.

#### Scenario: IFS shorter horizon
- **WHEN** selected IFS data ends before the expected seven-day horizon
- **THEN** the chart labels the actual available ending time or horizon
- **AND** the line is not padded with synthetic values.

#### Scenario: Quality flags and truncation
- **WHEN** station series contains non-ok `quality_flag` values or `truncated=true`
- **THEN** the UI marks affected intervals or displays an explicit quality/truncation indicator near the chart.

### Requirement: Hydro-met UI tests

The hydro-met MVP UI SHALL have automated coverage for route bootstrap, station selection, river selection, and unavailable states.

#### Scenario: Component and data-adapter tests
- **WHEN** frontend tests run
- **THEN** they cover latest-product bootstrap, API type usage, station series rendering, river `q_down` rendering, and empty/unavailable behavior.

#### Scenario: Browser smoke
- **WHEN** the MVP browser smoke runs with mocked or live QHH data
- **THEN** it opens `/hydro-met`, verifies station markers and river features are visible, selects at least one station and one river segment from the map or lists, and verifies both chart areas update from API-backed data.

