## MODIFIED Requirements

### Requirement: 气象代站作为可切换的 clustered-GeoJSON 图层

`M11MapLibreSurface` SHALL provide a meteorological-station primitive using a
MapLibre clustered-GeoJSON source (`cluster` enabled, with `clusters`,
`cluster-count`, and `met-stations-point` layers). The station primitive SHALL
be controlled by an independent station-overlay toggle rather than by an
exclusive `M11Layer` value, and station layers SHALL render above active
hydrology layers while those hydrology layers remain visible and clickable.
`interactiveLayerIds` MUST include `met-stations-point` and `clusters` whenever
the station overlay is enabled and has renderable features.

#### Scenario: 开启代站叠加时保留流量图层

- **WHEN** the active hydrology layer is `discharge`
- **AND** the user enables the meteorological-station overlay
- **THEN** the map MUST keep the discharge MVT source/layers registered and
  visible
- **AND** the map MUST also register the station clustered-GeoJSON source and
  `clusters` / `cluster-count` / `met-stations-point` layers.

#### Scenario: 代站渲染在河段上方

- **WHEN** station symbols and hydrology river lines overlap on screen
- **THEN** station cluster/point layers MUST render above the hydrology river
  layers
- **AND** station point or cluster hit detection MUST take precedence for the
  overlapped pixels.

#### Scenario: 河段仍可点击

- **WHEN** the station overlay is enabled
- **AND** the user clicks an exposed hydrology river line pixel not covered by a
  station point or cluster
- **THEN** the map MUST dispatch the river overlay click and open the river
  forecast workflow.

#### Scenario: 关闭图层后不渲染

- **WHEN** the user disables the meteorological-station overlay
- **THEN** the station source/layers are not registered
- **AND** station point/cluster layer ids are not included in the interactive
  layer set.

#### Scenario: 点击聚合簇展开

- **WHEN** the user clicks a meteorological-station cluster
- **THEN** the map MUST call the source cluster expansion zoom and fly to the
  expanded view
- **AND** it MUST NOT open a station forcing popup for the cluster itself.

### Requirement: 代站数据按可见或当前流域取数，分页至 client cap 且诚实标注 truncation

Station GeoJSON data SHALL be loaded through the independent
`stores/stationLayerData.ts` path and SHALL NOT pollute the `overviewData`
store. In overview mode the station request contexts SHALL come from the
currently visible basin contexts; in basin-detail mode they SHALL come from the
current basin context. For each basin context, station inventory loading SHALL
use the station API's supported basin/model scope (`basin_version_id` and/or
`model_id` derived from the visible/current basin context); it MUST NOT claim
source/cycle filtering for the station inventory endpoint. The store MUST
paginate station requests beyond the backend page limit up to a documented
client cap and expose `total`/`loaded`/`truncated` so UI and receipts honestly
mark incomplete station coverage. Source/cycle strictness remains required for
station-series curve loading after a station is clicked.

#### Scenario: 全国总览按可见流域加载代站

- **WHEN** the user is on the national overview with visible QHH and Heihe
  basin contexts
- **AND** the station overlay is enabled
- **THEN** station loading MUST request stations for those visible basin
  contexts
- **AND** the map MUST be able to display QHH and Heihe station features
  together, subject to the client cap and honest truncation state.

#### Scenario: 流域详情按当前流域加载代站

- **WHEN** the user is in basin-detail mode for QHH
- **AND** the station overlay is enabled
- **THEN** station loading MUST request QHH stations using the current basin's
  station inventory scope
- **AND** it MUST NOT mix station identities from another basin.

#### Scenario: 流域站点超 500 时分页取至 cap 并标注

- **WHEN** a visible basin has more station rows than the backend single-request
  limit
- **THEN** the station store MUST request additional pages until the documented
  client cap or total is reached
- **AND** if `loaded < total`, the UI/receipt MUST mark the station overlay as
  truncated.

#### Scenario: 源未解析时 station-series 不取数

- **WHEN** the selected source is `best` or `compare` and no concrete GFS/IFS
  source has been resolved
- **THEN** clicking a station MUST NOT request station-series data with `best` or
  `compare` as the source id
- **AND** the station curve window MUST show an honest waiting or unavailable
  state until a concrete source is available.

#### Scenario: 无可见流域上下文时诚实空态

- **WHEN** the station overlay is enabled but no visible basin context is
  available because basin bootstrap failed or returned empty
- **THEN** the frontend MUST NOT issue an unbounded all-stations request
- **AND** it MUST display an honest station-unavailable state.
