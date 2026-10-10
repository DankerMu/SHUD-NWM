## ADDED Requirements

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
