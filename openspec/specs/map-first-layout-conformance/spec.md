# map-first-layout-conformance Specification

## Purpose
TBD - created by archiving change m15-frontend-visual-conformance. Update Purpose after archive.
## Requirements
### Requirement: Map-first layout conformance
Map-first pages SHALL preserve documented panel widths, timeline height, and map dominance at supported desktop viewports. The V2 shell SHALL use the 84px site header and a 64px bottom control bar where applicable. The control bar SHALL sit 40px above the map bottom, reserving the bottom band for required map attribution; dependent legend/status-notice offsets SHALL preserve their separation from the bar.

#### Scenario: Full desktop
- **WHEN** viewport is 1920x1080 or 1440x900
- **THEN** overview and monitoring show the central map or primary operational canvas and applicable controls without incoherent overlap

#### Scenario: Collapsed breakpoint
- **WHEN** viewport is 1280x900
- **THEN** collapsible panels retain their default-left behavior and map/timeline usability

#### Scenario: Layout oracle
- **WHEN** a supported desktop viewport renders a map-first page
- **THEN** the header is 84px, the control bar is 64px where present, the document has no horizontal body scroll, and panels, control bar, legend and status notices do not obscure required map controls, attribution, charts or page actions
- **AND** the control bar and attribution rectangles do not intersect at 1920, 1440, 1280, 800 or 520px viewport widths, attribution remains visible, and the legend does not intersect attribution or the bar

### Requirement: Operational pages own vertical scrolling
Non-map operational pages SHALL expose overflowing content through an internal vertical scroll container while the window and fullscreen map shell remain non-scrolling.

#### Scenario: Short operational viewport
- **WHEN** an authorized user opens /ops, /monitoring or /system/model-assets at 1280x600 with overflowing content
- **THEN** mouse-wheel scrolling moves that page's content and can reach its bottom without scrolling the window

#### Scenario: Map fits available height
- **WHEN** / is opened at 1280x800 or 1280x600
- **THEN** the map fits below the header without window scrolling or a fixed minimum height clipping its bottom controls

