## ADDED Requirements

### Requirement: On a reach shared by two basin networks the click SHALL open the segment of the network with fewer segments

When more than one feature of the renderable overlay's hit layer lies under the pointer, the product
click-target resolution SHALL choose the first of them under this order, and SHALL NOT depend on the
order in which the map returned them except under rule 4. The network with fewer segments owns the
reach when every network under the pointer has a count; otherwise the network id decides:

1. **Counts, all or nothing.** The count of a feature is the smallest usable `segment_count` among the
   model records the overview has loaded for the feature's `river_network_version_id`; a count is
   usable when it is a finite number greater than zero. When every hit-layer feature under the pointer
   has a count, a feature with a smaller count comes before a feature with a larger one. When any of
   them has none (records not loaded, network absent from them, no usable count, no
   `river_network_version_id`), counts are not used at all.
2. **Network id.** Features with a `river_network_version_id` that rule 1 does not order are ordered
   by descending `river_network_version_id`, compared by UTF-16 code units and not by locale.
3. A feature without a `river_network_version_id` comes after every feature with one.
4. Features equal under 1 to 3 (features of one network) keep the order in which the map returned them.

The station cluster, station point and basin fill steps of the resolution and their priority over or
under the overlay step SHALL be unchanged. Where every hit-layer feature under the pointer belongs to
one network, the chosen feature SHALL be the first the map returned. The same resolution with the same
counts SHALL serve the product click handler and the river-click evidence hook; the station-locate
evidence hook SHALL call it with the same counts as well.

#### Scenario: The nested network owns the shared reach

- **WHEN** the features under the pointer are a segment of network `basins_b…` with 7,971 segments and
  a segment of network `basins_c…` with 25,971 segments (the network with fewer segments has the
  smaller id, so that the id alone would choose the other one)
- **THEN** the click opens the river window of the segment of the network with 7,971 segments
- **AND** it does so in either order in which the map returns the two features

#### Scenario: One network under the pointer behaves as before

- **WHEN** the features under the pointer are two segments of one network
- **THEN** the click opens the first one the map returned

#### Scenario: Equal counts are decided by the network id

- **WHEN** two networks under the pointer have the same segment count
- **THEN** the click opens the segment of the network whose `river_network_version_id` is larger by
  code-unit comparison, in either order in which the map returns them

#### Scenario: One network without a count switches the counts off

- **WHEN** one network under the pointer has a usable count and the other is absent from the loaded
  model records
- **THEN** the click opens the segment of the network whose `river_network_version_id` is larger,
  whichever of the two has the count

#### Scenario: No counts are loaded yet

- **WHEN** no model record has been loaded and two networks are under the pointer
- **THEN** the click opens the segment of the network whose `river_network_version_id` is larger

#### Scenario: A station still wins over a river

- **WHEN** stations are shown and a station point and two networks' segments are under the pointer
- **THEN** the click opens the station, as before

#### Scenario: The evidence hook follows the owner

- **WHEN** the river-click evidence hook is asked to locate a segment of the network with more segments
  at a point where a segment of a network with fewer segments is also under the pointer, and both
  counts are loaded
- **THEN** it rejects with `HOOK_POINT_OCCLUDED`
- **AND** asked for the segment of the network with fewer segments at the same point, it resolves
