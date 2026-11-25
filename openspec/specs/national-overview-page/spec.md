# national-overview-page Specification

## Purpose

The single map page at `/` as the national overview: the national extent with basin boundaries and hydrologic layers,
what a basin click does, the floating notices that report missing data, the basemap-unavailable status, and the V2.0
header brand. Routing, layout and the mobile form are stated by `single-map-shell-routing`,
`map-first-layout-conformance` and the `mobile-*` capabilities.

## Requirements

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

### Requirement: Overview reports missing data in floating notices

The overview (`apps/frontend/src/pages/OverviewPage.tsx`, `apps/frontend/src/components/map/m11MapRuntime.tsx`) SHALL report
missing or failed data in notices floating over the map, and SHALL keep rendering the map while a notice is shown. No
retry control is offered. Of the page's floating notices (`M11FloatingNotice`) one is shown at a time, and a status notice
of the met-station layer takes precedence over the empty-inventory notice; the map status notices are separate and can
show at the same time.

#### Scenario: No basin inventory

- **WHEN** the overview data has settled, the basin inventory is empty or the overview bootstrap failed, and no met-station layer status notice is shown
- **THEN** a floating notice (`m11-overview-empty`) states the reason
- **AND** the map is still rendered

#### Scenario: A business-layer map source fails

- **WHEN** the map reports an error that does not come from a basemap source and whose message does not mention `glyphs`
- **THEN** a map status notice (`m11-map-source-error`) shows the message, or `地图源加载失败，受影响图层暂不可用。` when it is empty
- **AND** the map is still rendered
