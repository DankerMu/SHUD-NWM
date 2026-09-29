## ADDED Requirements

### Requirement: Hovered discharge river segment is highlighted
The M11 map SHALL highlight the discharge river segment under the pointer with two line layers on the overlay source: `<overlay>-hover-halo` (white `#FFFFFF`, width 8, opacity 0.55) and `<overlay>-hover-line` (cyan `#22d3ee`, width 4.5, opacity 1), both filtered by `segmentFilter(hoveredSegmentId)`. Both the vector and the GeoJSON overlay branches SHALL register them, ordered after the hit layer and before the selected halo/line layers so the selected highlight renders above the hover highlight. The hovered id SHALL be `river_segment_id ?? segment_id` of the discharge hit feature; it SHALL become null when the pointer moves off river segments, onto a station, onto a basin fill, leaves the map, or the active overlay changes; and the map SHALL update the hover filter only when the id changes. Existing hover behaviour (pointer cursor, latest-product prefetch through `onOverlayHover`) and click/selection behaviour SHALL be unchanged.

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
