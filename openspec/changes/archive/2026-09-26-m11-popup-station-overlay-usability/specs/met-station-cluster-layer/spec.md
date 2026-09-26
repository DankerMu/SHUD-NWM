## MODIFIED Requirements

### Requirement: 气象代站作为可切换的 clustered-GeoJSON 图层

`M11MapLibreSurface` SHALL 提供气象代站 primitive，使用 MapLibre clustered-GeoJSON source（`cluster` 开启，含 `clusters` / `cluster-count` / `met-stations-point` 三层），并由 `LayerGroupControls` 暴露为可切换图层。`interactiveLayerIds` MUST 纳入 `met-stations-point` 与 `clusters`。
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

### Requirement: 代站数据按选中流域严格身份取数，分页至 client cap 且诚实标注 truncation

代站 GeoJSON 数据 SHALL 经独立 `stores/stationLayerData.ts` 按全国总览当前可见流域上下文加载（流域详情模式已退役，#2109 裁决 B），MUST NOT 污染 `overviewData` store。每个流域上下文的站点清单 SHALL 使用站点接口支持的 basin/model 范围（`basin_version_id`；源已解析为具体 GFS/IFS 时取该源 latest-product 的 `basin_version_id` 与 `model_id`），MUST NOT 宣称站点清单接口按 source/cycle 过滤。由于单次接口 `limit ≤ 500` 而流域站点数可超过 500（如 Heihe 1709），store MUST 分页（offset 翻页）拉取至一个明确的 client cap，并 MUST 暴露 `total`/`loaded`/`truncated` 供 UI 与 receipt 诚实标注（达到 cap 未取全时显式标 truncated，不得让"看似完整"的图层掩盖缺失）。点击代站后的 station-series 曲线取数仍要求 source/cycle 严格身份：源为 `best`/`compare` 时 MUST 先解析为具体 GFS/IFS 再取数。

#### Scenario: 流域站点超 500 时分页取至 cap 并标注
- **WHEN** a visible basin has more station rows than the backend single-request limit
- **THEN** the station store MUST request additional pages until the documented client cap or total is reached
- **AND** if `loaded < total`, the UI/receipt MUST mark the station overlay as truncated

#### Scenario: 按选中流域身份加载代站
- **WHEN** the user is on the national overview with visible QHH and Heihe basin contexts
- **AND** the station overlay is enabled
- **THEN** station loading MUST request stations for those visible basin contexts
- **AND** the map MUST be able to display QHH and Heihe station features together, subject to the client cap and honest truncation state

#### Scenario: 源未解析时不取数
- **WHEN** the selected source is `best` or `compare` and no concrete GFS/IFS source has been resolved
- **THEN** clicking a station MUST NOT request station-series data with `best` or `compare` as the source id
- **AND** the station curve window MUST show an honest waiting or unavailable state until a concrete source is available

#### Scenario: 全国总览无可用流域版本时开启代站图层的 honest 空态
- **WHEN** 处于全国总览、没有任何可解析的流域版本上下文，且用户开启"气象代站"图层（如经 `/meteorology`→`/?metStations=1` 进入）
- **THEN** MUST NOT 以无流域身份误打接口或拉全量，显示"暂无可用流域版本以加载气象代站"类 honest 空态；该判定不依赖任何 `basinId` URL 键（流域详情模式已退役，#2109 裁决 B）
