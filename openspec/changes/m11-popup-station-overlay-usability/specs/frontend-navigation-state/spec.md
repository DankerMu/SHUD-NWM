## MODIFIED Requirements

### Requirement: URL query restores shareable state

The system SHALL encode shareable overview and basin detail state in URL query
parameters. The active hydrology product layer SHALL be encoded separately from
the meteorological-station overlay. Station overlay visibility SHALL be encoded
as `metStations=1` when enabled and omitted when disabled. Stale
`layer=met-stations` URLs SHALL be accepted as a legacy alias and normalized to
a valid hydrology layer plus `metStations=1`.

#### Scenario: Overview query is restored

- **WHEN** an operator opens an overview URL containing valid `source`, `cycle`,
  `validTime`, `layer`, `basemap`, `metStations`, or station-overlay state
- **THEN** the overview page MUST initialize controls and map data from those
  parameters
- **AND** a valid hydrology `layer` MUST continue to drive hydrology MVT source
  selection independently of station overlay visibility.

#### Scenario: Basin detail query is restored

- **WHEN** an operator opens a basin detail URL containing valid
  `basinVersionId`, `segmentId`, `source`, `cycle`, `validTime`, `layer`,
  `metStations`, `warningLevel`, or search query
- **THEN** the basin detail page MUST initialize the selected version, segment,
  filters, station overlay state, and data requests from those parameters.

#### Scenario: Legacy met-stations layer query is normalized

- **WHEN** an operator opens a stale URL whose query contains
  `layer=met-stations`
- **THEN** the parser MUST treat that URL as station overlay enabled
- **AND** it MUST use `discharge` as the default active hydrology layer unless
  another valid hydrology layer is explicitly present
- **AND** serialization MUST emit the normalized state without
  `layer=met-stations`.

#### Scenario: Invalid query is corrected

- **WHEN** a URL query contains invalid source, layer, basemap, version,
  segment, valid-time, station-overlay, or search values
- **THEN** the page MUST fall back to a valid documented default
- **AND** it MUST avoid repeated URL update loops.
