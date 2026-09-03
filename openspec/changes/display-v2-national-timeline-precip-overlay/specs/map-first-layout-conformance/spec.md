## MODIFIED Requirements

### Requirement: Map-first layout conformance
Map-first pages SHALL preserve documented panel widths, timeline height, and map dominance at supported desktop viewports. From the V2.0 shell onward the top navigation is the 84px site header (`SiteHeader`, brand title + sponsor strip) and the bottom timeline is the 64px bottom control bar (`m11VisualTokens.timelineHeight`), which this change re-mounts on the fullscreen map shell.

#### Scenario: Full desktop
WHEN viewport is 1920x1080 or 1440x900
THEN overview, basin detail, and monitoring show page panels, central map or primary operational canvas, and bottom timeline where applicable without incoherent overlap

#### Scenario: Collapsed breakpoint
WHEN viewport is 1280x900
THEN collapsible panels use default-left behavior and maintain map/timeline usability

#### Scenario: Layout oracle
WHEN a supported desktop viewport renders a map-first page
THEN the top nav (site header) is 84px high, the bottom control bar (timeline) is 64px high where present and sits 16px above the viewport bottom, the document has no horizontal body scroll, and panels, the legend, the back button and status notices do not cover the control bar or required map controls, legends, charts, or page action controls
