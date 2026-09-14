## MODIFIED Requirements

### Requirement: M11 pages conform to GIS effect-image intent

The system SHALL treat `docs/spec/06_frontend_gis_design.md` and `docs/spec/06B_frontend_ui_design_spec.md` as normative visual acceptance references for M11 pages.

#### Scenario: National overview matches effect image 1 intent
- **WHEN** the national overview is rendered with representative data
- **THEN** it MUST visually match the effect-image-1 structure: top navigation, left basin/layer panel, central national map, right operational panel, and bottom timeline
- **AND** the map MUST be the dominant visual subject rather than a card or secondary panel

### Requirement: Visual states are implemented consistently

The system SHALL render loading, empty, error, disabled, and unavailable states according to the UI spec.

#### Scenario: Loading states preserve layout
- **WHEN** overview data is loading
- **THEN** panels MUST show skeletons or loading indicators sized to the eventual content
- **AND** the map MUST keep its container dimensions stable

#### Scenario: Empty and error states are scoped
- **WHEN** a panel, chart, layer, or list has no data or fails to load
- **THEN** the state MUST render inside the affected region with the documented icon/text/button treatment
- **AND** it MUST not collapse or overlap neighboring map and panel regions

### Requirement: Visual regression evidence is required

The system SHALL include automated or reviewable evidence that M11 pages remain aligned with the design spec.

#### Scenario: Agent-browser captures supported viewport screenshots
- **WHEN** frontend visual checks run
- **THEN** they MUST use `agent-browser` to capture overview screenshots at 1920x1080, 1440x900, and 1280x900 or the project's documented supported viewport set
- **AND** browser launch MUST pass Chromium flags as global arguments before the `open` subcommand, for example `agent-browser --args "--no-sandbox,--disable-dev-shm-usage,--disable-gpu" open <url>`
- **AND** screenshots MUST be saved with `agent-browser screenshot <path>`
- **AND** the checks MUST verify that top navigation, side panels, map area, timeline, popups/detail panels, and key controls do not overlap

#### Scenario: Visual drift is blocked
- **WHEN** screenshot or layout assertions show major drift from the effect-image layout or UI design spec
- **THEN** the related implementation issue MUST remain incomplete until the drift is fixed or explicitly documented as an approved design deviation

