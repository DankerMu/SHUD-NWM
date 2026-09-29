## MODIFIED Requirements

### Requirement: 气象代站作为可切换的 clustered-GeoJSON 图层

`M11MapLibreSurface` SHALL 提供气象代站 primitive，使用 MapLibre clustered-GeoJSON source（`cluster` 按 `m11StationClusterPolicy` 条件开启：站点数 ≤24 时关闭聚合，三层照常注册，`clusters`/`cluster-count` 的 `point_count` 过滤此时不命中；含 `clusters` / `cluster-count` / `met-stations-point` 三层），并由 `LayerGroupControls` 暴露为可切换图层。`interactiveLayerIds` MUST 纳入 `met-stations-point` 与 `clusters`。
The station primitive SHALL be controlled by an independent station-overlay toggle (`metStations`) rather than by an exclusive `M11Layer` value, and station layers SHALL render above active hydrology layers while those hydrology layers remain visible and clickable. `interactiveLayerIds` MUST include `met-stations-point` and `clusters` whenever the station overlay is enabled and has renderable features.

#### Scenario: 切换代站图层注册 source/layer
- **WHEN** 用户开启"气象代站"图层
- **THEN** 地图注册 clustered-GeoJSON source 与三层 layer，`met-stations-point`/`clusters` 进入可交互图层集

#### Scenario: 关闭图层后不渲染
- **WHEN** 用户关闭"气象代站"图层
- **THEN** 代站 source/layer 不再注册，地图不显示代站点

#### Scenario: 点击聚合簇展开
- **WHEN** 用户点击一个代站聚合簇（cluster）
- **THEN** 调用 source 的 cluster 展开 zoom 并 `flyTo`（运行时；测试以 stub 验证调用）
- **AND** it MUST NOT open a station forcing popup for the cluster itself

#### Scenario: 开启代站叠加时保留流量图层
- **WHEN** the active hydrology layer is `discharge`
- **AND** the user enables the meteorological-station overlay
- **THEN** the map MUST keep the discharge MVT source/layers registered and visible
- **AND** the map MUST also register the station clustered-GeoJSON source and `clusters` / `cluster-count` / `met-stations-point` layers

#### Scenario: 代站渲染在河段上方
- **WHEN** station symbols and hydrology river lines overlap on screen
- **THEN** station cluster/point layers MUST render above the hydrology river layers
- **AND** station point or cluster hit detection MUST take precedence for the overlapped pixels

#### Scenario: 河段仍可点击
- **WHEN** the station overlay is enabled
- **AND** the user clicks an exposed hydrology river line pixel not covered by a station point or cluster
- **THEN** the map MUST dispatch the river overlay click and open the river forecast workflow

#### Scenario: 河段 overlay 重新挂载后代站仍在上方
- **WHEN** the station overlay is enabled
- **AND** the discharge overlay source remounts because its `sourceKey` changed (timeline step) or because the overlay became renderable again after being null
- **THEN** after the resulting style update every discharge overlay layer (casing, main, hit, hover, selected) MUST sit below the station cluster/point layers
- **AND** the map MUST NOT reference a layer id that is not yet registered in the map style when restoring this order
- **AND** the restoration MUST be a no-op when the station layers already sit at the top of the style stack
