# map-first-layout-conformance Specification

## Purpose
TBD - created by archiving change m15-frontend-visual-conformance. Update Purpose after archive.

## Requirements

### Requirement: Map-first layout conformance
Map-first pages SHALL preserve documented panel widths, timeline height, and map dominance at supported desktop viewports, where a supported desktop viewport is one in desktop form (at least 768 CSS px wide and at least 500 CSS px high). From the V2.0 shell onward, in desktop form the top navigation is the 84px site header (`SiteHeader`, brand title + sponsor strip) and the bottom timeline is the 64px bottom control bar (`m11VisualTokens.timelineHeight`), which this change re-mounts on the fullscreen map shell; neither height applies in mobile form. Viewports in mobile form are governed by the `mobile-viewport-shell` and `mobile-map-overlay-layout` capabilities instead.

#### Scenario: Full desktop
- **WHEN** viewport is 1920x1080 or 1440x900
- **THEN** overview and monitoring show page panels, central map or primary operational canvas, and bottom timeline where applicable without incoherent overlap

#### Scenario: Collapsed breakpoint
- **WHEN** viewport is 1280x900
- **THEN** collapsible panels use default-left behavior and maintain map/timeline usability

#### Scenario: Layout oracle
- **WHEN** a supported desktop viewport renders a map-first page
- **THEN** the top nav (site header) is 84px high, the bottom control bar (timeline) is 64px high where present and sits 40px above the map bottom to reserve the attribution band, the document has no horizontal body scroll, and panels, the legend and status notices do not cover the control bar or required map controls, legends, charts, or page action controls
- **AND** the control bar itself does not intersect visible map attribution at 1920, 1440, 1280 or 800px viewport widths; dependent legend and notice offsets preserve separation from the raised bar

#### Scenario: Narrow width is mobile form
- **WHEN** viewport is 520x900
- **THEN** the page is in mobile form: the control bar does not intersect visible map attribution, and the header, panels and legend follow the mobile capabilities rather than the 84px header and always-expanded panels

### Requirement: Operational pages own vertical scrolling
Non-map operational pages SHALL expose overflowing content through an internal vertical scroll container while the window and fullscreen map shell remain non-scrolling.

#### Scenario: Short operational viewport
- **WHEN** an authorized user opens /ops, /monitoring or /system/model-assets at 1280x600 with overflowing content
- **THEN** mouse-wheel scrolling moves that page's content and can reach its bottom without scrolling the window

#### Scenario: Map fits available height
- **WHEN** / is opened at 1280x800 or 1280x600
- **THEN** the map fits below the header without window scrolling or a fixed minimum height clipping its bottom controls

### Requirement: The control bar's timeline SHALL stay inside the bar

Wherever the bottom control bar is the single-row 64px bar — desktop form and short-landscape mobile form — the timeline inside it SHALL be laid out within the bar's box: its bounding box SHALL NOT extend above the bar's top edge or below its bottom edge, both when a cycle is available and when none is. In desktop form without a cycle, the timeline's bottom-row text SHALL NOT extend horizontally past the timeline column.

#### Scenario: Timeline fits with a cycle

- **WHEN** the map page is shown in desktop form at 1280×900 or 768×1024 with an available cycle
- **THEN** the timeline's bounding box is at most 64px high and lies within the control bar's box

#### Scenario: Timeline fits without a cycle

- **WHEN** the map page is shown in desktop form at 768×1024 and no cycle is available
- **THEN** the timeline's bounding box lies within the control bar's box and does not intersect the map attribution, and its bottom-row text stays inside the timeline column without overlapping the disabled-reason text

#### Scenario: Timeline fits in short landscape

- **WHEN** the map page is shown in short-landscape mobile form with an available cycle
- **THEN** the timeline's bounding box is at most 64px high and lies within the control bar's box
