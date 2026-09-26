## MODIFIED Requirements

### Requirement: URL query restores shareable state

The system SHALL encode shareable overview state in URL query parameters. Basin-detail state is retired (#2109 decision B); `basinId` is not a query key.
The active hydrology product layer SHALL be encoded separately from the meteorological-station overlay. Station overlay visibility SHALL be encoded as `metStations=1` when enabled and omitted when disabled. Stale `layer=met-stations` URLs SHALL be accepted as a legacy alias and normalized to a valid hydrology layer plus `metStations=1`.

#### Scenario: Overview query is restored
- **WHEN** an operator opens an overview URL containing valid `source`, `cycle`, `validTime`, `layer`, or `basemap` state
- **THEN** the overview page MUST initialize controls and map data from those parameters
- **AND** a `metStations=1` parameter MUST initialize the station overlay as enabled
- **AND** a valid hydrology `layer` MUST continue to drive hydrology MVT source selection independently of station overlay visibility

#### Scenario: Invalid query is corrected
- **WHEN** a URL query contains invalid source, layer, basemap, version, segment, or valid-time values
- **THEN** the page MUST fall back to a valid documented default
- **AND** it MUST avoid repeated URL update loops
- **AND** an invalid station-overlay or search value MUST likewise fall back to its documented default (overlay disabled, no search)

#### Scenario: Retired basinId key is dropped
- **WHEN** a URL query contains `basinId`
- **THEN** the page MUST replace the URL with the normalised query without `basinId` and render the national overview

#### Scenario: Legacy met-stations layer query is normalized
- **WHEN** an operator opens a stale URL whose query contains `layer=met-stations`
- **THEN** the parser MUST treat that URL as station overlay enabled
- **AND** it MUST use `discharge` as the default active hydrology layer unless another valid hydrology layer is explicitly present
- **AND** serialization MUST emit the normalized state without `layer=met-stations`
