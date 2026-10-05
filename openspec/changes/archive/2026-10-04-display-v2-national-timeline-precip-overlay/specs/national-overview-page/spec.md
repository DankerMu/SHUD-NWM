## ADDED Requirements

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

## MODIFIED Requirements

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
