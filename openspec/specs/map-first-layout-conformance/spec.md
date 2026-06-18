# map-first-layout-conformance Specification

## Purpose
TBD - created by archiving change m15-frontend-visual-conformance. Update Purpose after archive.
## Requirements
### Requirement: Map-first layout conformance
Map-first pages SHALL preserve documented panel widths, timeline height, and map dominance at supported desktop viewports.

#### Scenario: Full desktop
WHEN viewport is 1920x1080 or 1440x900
THEN overview, basin detail, flood alerts, and monitoring show top nav, page panels, central map or primary operational canvas, and bottom timeline where applicable without incoherent overlap

#### Scenario: Collapsed breakpoint
WHEN viewport is 1280x900
THEN collapsible panels use default-left behavior and maintain map/timeline usability

#### Scenario: Layout oracle
WHEN a supported desktop viewport renders a map-first page
THEN the top nav remains 56px high, the bottom timeline remains 64px high where present, the document has no horizontal body scroll, and panels do not cover required map controls, legends, charts, or page action controls

