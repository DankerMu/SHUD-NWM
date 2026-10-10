## MODIFIED Requirements

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
