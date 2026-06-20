<!--
  Modification rationale (2026-06-20): the product decision removes
  `water-level` as a supported hydro variable. The MODIFIED requirement below
  drops the prior `water-level delta when available` clause from the rendered
  field list. No standalone `## REMOVED Requirements` block is used because the
  dropped clause was a phrase inside an existing scenario, not its own
  `### Requirement:` header in the live spec (and a REMOVED block here would
  fail `openspec validate --strict`).
-->

## MODIFIED Requirements

### Requirement: Selected segment detail provides forecast context

The system SHALL show selected segment metadata, current forecast values, source/cycle information, quality status, trend preview, and handoff actions.

#### Scenario: Segment detail renders available fields
- **WHEN** selected segment detail and forecast data are available
- **THEN** the detail panel MUST show `river_segment_id`, basin name, model identifier when available, catchment area or length when available, current Q, return-period level, forecast valid time, source, and cycle time

#### Scenario: Trend sparkline is shown for selected segment
- **WHEN** recent or forecast trend points are available for the selected segment
- **THEN** the right panel MUST show a compact sparkline for the segment
- **AND** it MUST mark the current value and trend direction when those values can be derived

#### Scenario: Segment handoff actions are explicit
- **WHEN** a segment is selected
- **THEN** the detail panel MUST expose "查看详情" as a handoff to the future full-screen forecast detail route or an implemented existing detail route
- **AND** it MUST expose "对比预报" that overlays or requests comparison data when IFS/GFS comparison data exists, otherwise shows a disabled unavailable state
