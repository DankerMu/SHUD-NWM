# frontend-visual-conformance Specification

## Purpose
TBD - created by archiving change m11-overview-basin-drilldown. Update Purpose after archive.

## Requirements

### Requirement: M11 pages conform to GIS effect-image intent

The system SHALL treat `docs/spec/06_frontend_gis_design.md` and `docs/spec/06B_frontend_ui_design_spec.md` as normative visual acceptance references for M11 pages.

#### Scenario: National overview matches effect image 1 intent
- **WHEN** the national overview is rendered with representative data
- **THEN** it MUST visually match the effect-image-1 structure: top navigation, left basin/layer panel, central national map, right operational panel, and bottom timeline
- **AND** the map MUST be the dominant visual subject rather than a card or secondary panel

### Requirement: UI tokens and component proportions follow the design spec

The system SHALL implement the documented visual tokens and component proportions or map them explicitly to existing project tokens.

#### Scenario: Layout tokens are applied
- **WHEN** overview is rendered at supported desktop viewports
- **THEN** the top navigation (the `SiteHeader` brand header) MUST be 84px high
- **AND** side panels MUST use the documented 280px left and 320-360px right proportions where viewport size permits
- **AND** the bottom timeline MUST use the documented 64px height

#### Scenario: Component styling is consistent
- **WHEN** panels, cards, buttons, inputs, toggles, tags, tooltips, and popup cards render
- **THEN** they MUST follow the documented font sizes, 4px spacing scale, 4px/8px radii, shadows, status colors, and icon sizing
- **AND** any existing project token substitution MUST be documented in code or developer notes

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

### Requirement: Font utilities SHALL take effect on form controls

The frontend's global stylesheet SHALL NOT declare a font reset for `button`, `input`, `select` or `textarea` outside a cascade layer. Font-size, font-weight, font-family and line-height utilities written on those elements SHALL determine their computed style without an `!important` modifier; the reset that makes form controls inherit by default is the one Tailwind's preflight provides inside `@layer base`.

#### Scenario: A font-size utility on a button wins over the ancestor's size
- **WHEN** a `button` carries a non-important font-size utility and its ancestor has a different font size
- **THEN** the button's computed font size equals the utility's value

#### Scenario: The built stylesheet has no unlayered font reset for form controls
- **WHEN** the production build's CSS is inspected
- **THEN** no rule outside an `@layer` block sets `font: inherit` on `button`, `input`, `select` or `textarea`

#### Scenario: The mobile 16px floor holds without important modifiers
- **WHEN** the display entry is rendered in mobile form
- **THEN** every visible `select`, select trigger and `input` covered by the mobile 16px floor has a computed font size of at least 16px
- **AND** the shipped base components reach it without an `!important` font-size utility
