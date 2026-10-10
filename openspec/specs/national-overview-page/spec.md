# national-overview-page Specification

## Purpose
TBD - created by archiving change m11-overview-basin-drilldown. Update Purpose after archive.

## Requirements

### Requirement: National overview is the default operational entry

The system SHALL render a national overview page as the default operator entry with global navigation, left control panel, central national map, right summary panel, and bottom timeline.

#### Scenario: Default route opens national overview
- **WHEN** an operator opens `/`
- **THEN** the application MUST render the national overview page
- **AND** the global navigation MUST expose 全国总览, 水文预报, 洪水预警, 产品监控, and any existing administration entry that is available in the app
- **AND** existing 洪水预警 and 产品监控 routes MUST remain reachable

#### Scenario: Overview layout follows map-first structure
- **WHEN** the overview page is rendered on a desktop viewport
- **THEN** the central map MUST occupy the primary visual area
- **AND** the left panel MUST contain basin and layer controls
- **AND** the right panel MUST contain basemap, legend, forecast run, and warning summaries
- **AND** the bottom timeline MUST remain visible without covering map popups or primary controls
- **AND** the layout MUST follow the effect-image-1 structure and the UI spec's 84px top header (V2.0 brand header, replacing the former 56px navigation), approximately 280px left panel, 320-360px right panel, and 64px bottom control bar (`m11VisualTokens.timelineHeight`) where viewport size permits

#### Scenario: Overview responsive behavior follows UI spec
- **WHEN** the overview page is rendered at 1920px or wider
- **THEN** both side panels MUST be expanded in a three-column map-first layout
- **WHEN** the page is rendered at 1440-1919px
- **THEN** the page MUST remain a three-column layout with the right panel allowed to shrink to approximately 280px
- **WHEN** the page is rendered at 1280-1439px
- **THEN** side panels MUST be collapsible and the map MUST remain the primary surface
- **WHEN** the page is rendered below the supported minimum
- **THEN** it MUST show a documented degraded layout or viewport warning instead of overlapping controls

### Requirement: Basin tree controls national map visibility

The system SHALL provide a basin tree grouped by major basins and allow operators to control visible basin boundaries and river networks.

#### Scenario: Basin tree renders available basins
- **WHEN** basin inventory data is loaded
- **THEN** the left panel MUST show basins grouped by top-level basin when hierarchy metadata is available
- **AND** each visible basin row MUST include a checkbox or equivalent toggle
- **AND** the panel MUST provide all-select and none-select actions

#### Scenario: Basin visibility changes map layers
- **WHEN** an operator hides a basin from the tree
- **THEN** that basin's boundary and basin-scoped river network MUST be removed or visually hidden from the overview map
- **AND** the URL or store state MUST retain the visibility selection while the operator remains on the page

### Requirement: Overview map displays national basin context

The system SHALL display the national extent with basin boundaries, basin labels, and hydrologic risk layers where data is available.

#### Scenario: National map initializes at China extent
- **WHEN** the overview map loads
- **THEN** it MUST initialize to a national China extent matching approximately 73E-135E and 18N-53N
- **AND** it MUST render basin boundaries with translucent fill when boundary data is available
- **AND** it MUST render basin labels or an accessible substitute when label geometry is available

#### Scenario: National hydrologic risk layer renders
- **WHEN** river network or discharge data is available for the active layer/source/time
- **THEN** the overview map MUST render the national river or risk layer using the active hydrologic color scale
- **AND** changing the related layer toggle MUST update or hide the corresponding map layer
- **AND** unavailable river/risk data MUST show a scoped unavailable state rather than fake geometry or values

#### Scenario: Basin click fits the camera and stays on the overview
- **WHEN** an operator clicks a visible basin on the overview map
- **THEN** the map camera MUST fit that basin's bounding box
- **AND** the page MUST stay on the national overview: no basin drill-down mode, no route change, and no `basinId` written to the URL
- **AND** every other basin MUST remain rendered and clickable

### Requirement: Right-side summaries provide operational links

The system SHALL provide right-side operational summary cards for forecast run status and display coverage status.

#### Scenario: Forecast run summary links to monitoring
- **WHEN** pipeline or monitoring summary data is available
- **THEN** the right panel MUST show completed forecast cycles today and currently running work when the data can be derived
- **AND** clicking the forecast run summary MUST navigate to the existing product monitoring page

#### Scenario: Display coverage summary links to monitoring
- **WHEN** display coverage summary data is available
- **THEN** the right panel MUST show basin coverage count and latest update time
- **AND** clicking the summary MUST navigate to monitoring with source/cycle context when available

### Requirement: Overview handles degraded data states

The system SHALL render useful degraded states when backend data is missing or partial.

#### Scenario: No basin inventory
- **WHEN** the basin inventory request returns an empty list
- **THEN** the overview MUST show an empty basin state in the left panel
- **AND** the map MUST remain usable with any available non-basin layers

#### Scenario: Basin has no published version
- **WHEN** a basin exists but has no published or active basin version
- **THEN** the basin row and popup MUST show a version-unavailable state

#### Scenario: Partial summary failure
- **WHEN** one summary request fails but basin inventory or map data succeeds
- **THEN** the page MUST render the successful sections
- **AND** the failed summary card MUST show a scoped error or unavailable state without replacing the whole page

#### Scenario: Overview map source fails
- **WHEN** a map source or layer request fails
- **THEN** the affected layer MUST show an inline map or panel error with retry affordance when possible
- **AND** other successful layers and controls MUST remain usable

### Requirement: Header brand identity for V2.0
The site header SHALL display the title `全国水文模拟系统（V2.0）` (full-width parentheses) in bold at a larger size than the V1 header, and SHALL display the sponsor logo strip at a larger size; the header height MAY increase to accommodate both.

#### Scenario: Title text and weight
- **WHEN** the application shell renders
- **THEN** the header title text equals `全国水文模拟系统（V2.0）` exactly (full-width `（` and `）`)
- **AND** the title uses a bold weight and a font size of at least 28px

#### Scenario: Sponsor strip enlarged
- **WHEN** the header renders on a `lg` viewport
- **THEN** the sponsor image renders at a height of at least 56px with `object-contain`
- **AND** the header height is exactly 84px (the V2.0 value that replaces the former 56px navigation in `map-first-layout-conformance`) so the title and sponsor strip do not clip

### Requirement: The basemap-unavailable status SHALL NOT be cleared by overview data settling

Once a basemap tile failure has raised the map-source-error status with the basemap-unavailable notice, the status SHALL NOT be cleared because the layer catalogue arrives or the valid time settles or changes. The notice SHALL be cleared when the user switches to a different basemap. A business-layer source error SHALL still take precedence over the basemap notice and SHALL still be cleared when the layer inputs change, after which a basemap whose failure was recorded SHALL show its notice again.

#### Scenario: Overview data settles after the basemap tiles have failed

- **WHEN** basemap tile requests have failed and the status shows the basemap-unavailable notice
- **AND** the overview bootstrap data arrives afterwards and the valid time is corrected
- **THEN** the status shows the basemap-unavailable notice without having been removed in between

#### Scenario: The user switches basemap

- **WHEN** the basemap-unavailable notice is shown and the user selects a different basemap
- **THEN** the notice is cleared until a tile of the newly selected basemap fails
