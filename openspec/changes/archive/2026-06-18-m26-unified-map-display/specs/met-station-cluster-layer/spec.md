---
status: archived
current_authority:
  - openspec/specs/single-map-shell-routing/spec.md
  - openspec/specs/legacy-display-page-retirement/spec.md
  - openspec/specs/inplace-overview-basin-detail/spec.md
  - openspec/specs/map-feature-popups/spec.md
  - openspec/specs/met-station-cluster-layer/spec.md
  - docs/runbooks/display-readonly-live-mvt.md
  - docs/runbooks/two-node-deployment-overview.md
superseded_by:
  - openspec/specs/single-map-shell-routing/spec.md
  - openspec/specs/legacy-display-page-retirement/spec.md
  - openspec/specs/inplace-overview-basin-detail/spec.md
  - openspec/specs/map-feature-popups/spec.md
  - openspec/specs/met-station-cluster-layer/spec.md
status_since: 2026-06-24
archive_scope: whole-document
retained_for: "audit evidence for the archived M26 met-station cluster layer delta"
---

## ADDED Requirements

### Requirement: 气象代站作为可切换的 clustered-GeoJSON 图层

`M11MapLibreSurface` SHALL 提供气象代站 primitive，使用 MapLibre clustered-GeoJSON source（`cluster` 开启，含 `clusters` / `cluster-count` / `met-stations-point` 三层），并由 `LayerGroupControls` 暴露为可切换图层（现 `meteorologyPlaceholders` 占位转为可切换的 `met-stations`）。`interactiveLayerIds` MUST 纳入 `met-stations-point` 与 `clusters`。

#### Scenario: 切换代站图层注册 source/layer
- **WHEN** 用户开启"气象代站"图层
- **THEN** 地图注册 clustered-GeoJSON source 与三层 layer，`met-stations-point`/`clusters` 进入可交互图层集

#### Scenario: 关闭图层后不渲染
- **WHEN** 用户关闭"气象代站"图层
- **THEN** 代站 source/layer 不再注册，地图不显示代站点

#### Scenario: 点击聚合簇展开
- **WHEN** 用户点击一个代站聚合簇（cluster）
- **THEN** 调用 source 的 cluster 展开 zoom 并 `flyTo`（运行时；测试以 stub 验证调用）

### Requirement: 代站数据按选中流域严格身份取数，分页至 client cap 且诚实标注 truncation

代站 GeoJSON 数据 SHALL 经独立 `stores/stationLayerData.ts`（薄 store 复用 `loadHydroMetBootstrap`→`fetchHydroMetStations`）按当前选中流域 latest-product 的严格身份（model_id/basin_version_id/source/cycle_time）加载，MUST NOT 污染 `overviewData` store。由于单次接口 `limit ≤ 500` 而流域站点数可超过 500（如 Heihe 1709），store MUST 分页（offset 翻页）拉取至一个明确的 client cap，并 MUST 暴露 `total`/`loaded`/`truncated` 供 UI 与 receipt 诚实标注（达到 cap 未取全时显式标 truncated，不得让"看似完整"的图层掩盖缺失）。源为 `best`/`compare` 时 MUST 先解析为具体 GFS/IFS 再取数。

#### Scenario: 流域站点超 500 时分页取至 cap 并标注
- **WHEN** 选中 Heihe（1709 站）、源解析为 GFS
- **THEN** store 分页拉取至 client cap，暴露 `total=1709`/`loaded`/`truncated`；若未取全，UI 与 receipt MUST 显式标注 truncated

#### Scenario: 按选中流域身份加载代站
- **WHEN** 选中流域为 Qhh（386 站）、源解析为 GFS
- **THEN** 代站 store 以 Qhh GFS latest-product 身份取全部 386 站（≤cap，`truncated=false`），渲染该流域代站

#### Scenario: 源未解析时不取数
- **WHEN** 源为 best 且尚未解析为具体 GFS/IFS
- **THEN** 代站图层不发起以 `best` 为源的取数，等待 `resolvedSource`

#### Scenario: 全国总览（无 basinId）开启代站图层的 honest 空态
- **WHEN** 处于全国总览（`basinId` 为空）且用户开启"气象代站"图层（如经 `/meteorology`→`/?layer=met-stations` 进入）
- **THEN** MUST NOT 以无流域身份误打接口或拉全量，显示"请选择流域以加载气象代站"类 honest 空态；选中某流域后再按该流域身份加载

### Requirement: 代站图层为未来 station-MVT 预留切换抽象

代站 primitive SHALL 以 `layerId`/`source` 抽象组织，使未来切换到后端 station-MVT 点图层瓦片端点时无需重写交互/popup 逻辑。本变更 MUST NOT 实现后端 station-MVT 端点（属解耦平行 issue）。

#### Scenario: 抽象预留不实现后端瓦片
- **WHEN** 审阅本变更代站图层实现
- **THEN** primitive 通过 `layerId`/`source` 抽象引用数据源，且不包含后端 station-MVT 端点实现（仅 clustered-GeoJSON）
