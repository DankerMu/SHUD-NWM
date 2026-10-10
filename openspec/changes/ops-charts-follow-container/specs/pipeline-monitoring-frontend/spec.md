## ADDED Requirements

### Requirement: Monitoring charts SHALL follow their container's width

The charts on the monitoring page — the queue donut, the trend lines and the stage-duration bars — SHALL be redrawn at their container's current width whenever that width changes after the chart has rendered, including a change that happens immediately after the chart is first rendered. A chart that is no longer rendered SHALL stop observing its container.

#### Scenario: Container changes size right after the first render

- **WHEN** a chart's container reports a size different from the chart's canvas, including on the first size notification after the chart became ready
- **THEN** the chart is resized to its container

#### Scenario: Viewport resized on a loaded page

- **WHEN** the viewport of an already loaded monitoring page in its single-column layout changes between 750×342 and 390×664
- **THEN** every chart canvas ends up as wide as its container and the page's scroll container does not overflow horizontally

#### Scenario: Chart removed

- **WHEN** a trend line or stage-duration chart loses its data and stops rendering
- **THEN** its container observer is disconnected
