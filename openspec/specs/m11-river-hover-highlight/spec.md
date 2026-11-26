# m11-river-hover-highlight Specification

## Purpose
M11 首页地图上，discharge 河段在指针悬停时给出视觉高亮（白色光晕 + 青线，压在选中橙线之下），让用户在点击前就能看清将打开哪一条河段的曲线；悬停状态只影响 discharge overlay 的两层 filter，不改变点击、选中与预取行为。

## Requirements

### Requirement: Hovered discharge river segment is highlighted
The M11 map SHALL highlight the discharge river segment under the pointer with two line layers on the overlay source: `<overlay>-hover-halo` (white `#FFFFFF`, width 8, opacity 0.55) and `<overlay>-hover-line` (cyan `#22d3ee`, width 4.5, opacity 1), both filtered by `segmentFilter(hoveredSegmentId)`. Both the vector and the GeoJSON overlay branches SHALL register them, ordered after the hit layer and before the selected halo/line layers so the selected highlight renders above the hover highlight. The hovered feature SHALL be the discharge hit-layer feature that the product click-target resolution would choose among the hit-layer features under the pointer (on a reach shared by two basin networks, the segment of the network with fewer segments when every network under the pointer has a segment count), and the hovered id SHALL be `river_segment_id ?? segment_id` of that feature; `onOverlayHover` SHALL receive that same feature; it SHALL become null when the pointer moves off river segments, onto a station, onto a basin fill, leaves the map, or the active overlay changes; and the map SHALL update the hover filter only when the id changes. Existing hover behaviour (pointer cursor, latest-product prefetch through `onOverlayHover`) and click/selection behaviour SHALL be unchanged.

#### Scenario: hover follows the pointer
- **WHEN** the pointer moves over discharge segment A, then segment B, then empty map
- **THEN** the hover layers' filter targets A, then B, then matches no feature

#### Scenario: repeated moves on one segment
- **WHEN** the pointer moves several times within segment A
- **THEN** the hover state updates once

#### Scenario: selection stays on top
- **WHEN** a segment is both hovered and selected
- **THEN** the selected halo/line layers render above the hover halo/line layers

#### Scenario: prefetch preserved
- **WHEN** the pointer hovers a discharge segment
- **THEN** `onOverlayHover` still receives the interaction and the latest-product prefetch still runs

#### Scenario: hover shows what the click opens on a shared reach
- **WHEN** the pointer is over a reach on which segments of two networks with loaded segment counts lie, whichever order the map returns them in
- **THEN** the hover layers' filter targets the segment of the network with fewer segments
- **AND** `onOverlayHover` receives that segment's feature, and a click at that point opens that segment
