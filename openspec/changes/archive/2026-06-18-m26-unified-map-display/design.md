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
retained_for: "audit evidence for M26 display-route convergence"
---

## Context

展示端当前是"多页 + 顶部导航"形态，且存在两套并行地图实现：

- **M11 体系（可扩展、瓦片）**：`OverviewPage.tsx`（全国总览，`useOverviewDataStore.loadOverview(query)`，basin 可见性树 Heihe/Qhh）、`BasinDetailPage.tsx`（流域详情，`useParams().basinId` + `loadBasinDetail(basinId, query)`，河段列表/选中河段/趋势/对比/RP 三态）、`M11Shell.tsx`（`M11Layout` 三栏 + 时间轴容器）、`M11Controls.tsx`（`SourceScenarioControls`/`LayerGroupControls`/`LayerLegendPanel`/`M11Timeline`，气象图层目前为 `meteorologyPlaceholders` 占位）、`M11MapLibreSurface.tsx`（MapLibre：MVT overlay primitive + 流域边界 GeoJSON + **河网从 `basinSegments` GeoJSON 渲染** + 选中河段 primitive；交互经 `findEventFeature` 分发 `M11MapOverlayInteraction`；相机 `fitTo`/`flyTo`；客户端 geometry 预算守卫）。数据 store `stores/overviewData.ts` 的取数键完全由 `M11QueryState` 派生（`requestScopeQueryKey`），`loadBasinDetail` 额外吃路由 `basinId`。
- **hydroMet 体系（玩具、不可扩展）**：`HydroMetPage.tsx`（2496 行，DOM marker + 拉全量列表 + 搜索/分页/stream_order 过滤/变量·QC 筛选）。其依赖的 honest-display 库是有价值、需保留的：`bootstrap.ts`（latest-product 严格身份校验 → 5 态 status；按产品身份取 stations/river-segments）、`lib/hydroMet/stationSeries.ts`（六要素 station-series + 身份校验）、`lib/hydroMet/riverForecast.ts`（q_down forecast-series + `validateHydroMetRiverForecastForChart`：缺任一身份即 `ok:false` **不绘图**）、`ReturnPeriodSection.tsx`（`ProductStatusBar` 三桶 + `productReady` 门控 + `returnPeriodTone` 二态 + 静态分级图例）。

node-27 `display_readonly` 实测（走隧道打 display API）：`/basins?has_display_product=true`→Qhh+Heihe；`/mvp/qhh/latest-product?source=GFS|IFS&basin_id=…`→真实产物（**best 不支持，仅 GFS/IFS**）；`/met/stations`、`/{id}/series`、river-segments（FeatureCollection 带 geometry）、`/{seg}/forecast-series`（需 `river_network_version_id`）、`/runs`（85 条含失败）均可读。但 **`/api/v1/layers`→`[]`、river-network 瓦片→HTTP 424、hydro 瓦片→HTTP 409**：MVT 瓦片管线在 `display_readonly` 环境未点亮（live PostGIS MVT 不可用 / 图层未注册）。

## Goals / Non-Goals

**Goals:**

- 整个展示端收敛为**一张全屏地图、无顶部导航**；全国总览↔流域详情同图 zoom 就地切换；气象代站/河段流量/重现期为可切换图层；点击要素 popup 出按需曲线。
- **可扩展**：架构对接已有 M16 瓦片管线；代站图层 primitive 为未来 station-MVT 预留 source 抽象；新增流域 DB 注册即自动出现（复用 `has_display_product` 发现）。
- 删除 2496 行不可扩展玩具页，消除两套并行地图的重复；保留全部 honest-display 不变量。
- 运维 `/ops`/`/monitoring` 收缩为 RBAC 直链，不在主交付。

**Non-Goals:**

- 不产出洪水重现期真实产品、不造假河段/假曲线。
- 不新建后端 station-MVT 端点、不在本变更排查 display_readonly 的 424/409（均另开 issue）。
- 不改 DB schema、不改 display_readonly 后端边界、不删 `/ops` 代码。
- 本期 zoom 自动驱动详情列为后续，先做点选驱动（避免相机回环 + 无头 WebGL 测试复杂度）。

## Decisions

### D1. 单页落点 = 改造 `OverviewPage` 为 `DisplayMapPage`

`OverviewPage` 已承载 `M11Layout` + overview 取数 + basin 可见性树，改造面最小。把 `BasinDetailPage` 的详情面板（河段列表 `SegmentDiscoveryPanel`、选中河段 `SelectedSegmentPanel`、趋势、对比、RP 三态）抽到 `components/m11/BasinDetailPanels.tsx` 并入单页，按 `state.basinId` 双模式渲染。

**备选**：新建独立页重写——否决（与 M11 取数/布局重复，违背 DRY）。

### D2. 总览↔详情就地化 = `basinId` 进 query，不进路由

`lib/m11/queryState.ts` 的 `M11QueryState` 新增 `basinId: string | null`（纳入 parse/serialize/默认，identifier 白名单校验）。`stores/overviewData.ts` 的 `loadBasinDetail` 签名保持 `(basinId, query)`，由单页传 `state.basinId`；snapshot 匹配函数按 query 内 basinId 判定。**store 内部取数键与 honest 契约不重写**——仅改 basinId 入口（路由 param → query）。选流域：点 `m11-basin-fill` 或可见性树"进入分析"→`handleQueryChange({ basinId })`（替换 `navigate('/basins/:id')`）+ `fitTo` basin bbox；"返回总览"→`handleQueryChange({ basinId: null, segmentId: null })`。

**风险（最高）**：snapshot 匹配键含 basinId 后可能数据闪烁/不加载；basinId 在 query 与原 param 的归一化差异；OverviewPage 现有测试对"两页"模型的强假设。**缓解**：保持 store 内部逻辑不变，先补 store 单测覆盖"basinId-from-query"，再改页面。

### D3. 代站图层 = clustered-GeoJSON primitive（瓦片为未来）

`M11MapLibreSurface` 新增 `M11StationClusterPrimitive`：react-map-gl `<Source type="geojson" cluster clusterRadius=50 clusterMaxZoom=14>` + 三层 `<Layer>`（`clusters` circle / `cluster-count` symbol / `met-stations-point` circle）。数据经**新建 `stores/stationLayerData.ts`**（薄 store 包 `loadHydroMetBootstrap`→`fetchHydroMetStations`，按选中流域 latest-product 严格身份），不污染 overviewData store。单次接口 `limit≤500` 而流域站点可超 500（Heihe 1709），store **分页（offset）拉取至明确 client cap 并暴露 `total/loaded/truncated`** 供 UI/receipt 诚实标注；全国总览无 `basinId` 时不取数、显示"选择流域"honest 空态。`interactiveLayerIds` 纳入 `met-stations-point`/`clusters`。点 cluster→运行时 `getSource().getClusterExpansionZoom`+`flyTo`（测试用 stub）。

**为何不现在做后端 station-MVT**：当前 2 流域约 2095 站，clustered-GeoJSON（limit≤500/流域）足够；全国万级才需点图层瓦片端点——另开 backend issue，primitive 以 `layerId/source` 抽象预留切换位。

### D4. 两类 popup = maplibre `Popup` + echarts，复用 honest-display 库

新增 `components/map/M11RiverForecastPopup.tsx`（点河段→`loadHydroMetRiverForecast`+`validateHydroMetRiverForecastForChart`→`ForecastChart` q_down + `ReturnPeriodSection` 三态；`ok:false` 显示原因不画曲线）与 `M11StationForcingPopup.tsx`（点代站→`loadHydroMetStationSeries`+身份校验→6 个 echarts；身份不符空态）。`ReturnPeriodSection` 从 `pages/hydroMet/` 迁到 `components/m11/`（保留导出名以减小测试改动）。source 用 `state.source`，`best`/`compare` 须先经 `sourceSelection.resolvedSource` 落为 GFS/IFS（hydroMet 库只接受 GFS/IFS），未解析时 popup 显"等待 Best Available 解析"空态。

### D5. 去导航 + 路由收敛（最小改 AppShell）

`AppShell` 删 `<NavBar/>`（保留 `ToastProvider`/`<main>`/role override，可在角落留低调"运维"直链给 operator+）。`App.tsx`：`/`→`DisplayMapPage`；`/overview`/`/forecast`/`/flood-alerts`(→`?layer=flood-return-period`)/`/hydro-met`/`/meteorology`(→`?layer=met-stations`)→`<Navigate>` 到 `/`（保留可映射 query）；`/basins/:basinId`→`/?basinId=:basinId`；`/ops`/`/monitoring`/`/system/model-assets` 不动。`lib/m11/visualTokens.ts` 的 `--m11-nav-height` 去 nav 后归 0，`M11Layout` 高度改全屏。

### D6. honest-display 不变量保持，不为观感伪造图层

`buildM11RegisteredOverlay`/`m11SelectedLayerUnavailableReason` 的"未注册即不渲染 overlay + 显示提示"是 honest 红线，**保留**。实机 424/409/layers 空属环境问题，本变更不掩盖；当前规模靠 GeoJSON 河网兜底渲染，全国态 overlay 待后端图层注册自动点亮。

## Risks / Mitigations

- **R1 数据 store 就地化（最高）**：见 D2，先补 store 单测护栏 + 保持内部逻辑不变。
- **R2 无头 WebGL 不可用**：地图渲染/cluster/popup 定位/相机断言全走 react-map-gl mock（`m11FitBoundsCalls`/`m11FlyToCalls`/`data-*`/`getSource` stub）；live 视觉验证用 node-27 + `--headed`。
- **R3 旧路由重定向遗漏 query**：重定向组件读 `useParams`+`useLocation` 合成目标 query，AppRoutes 测试逐路由断言落点。
- **R4 全屏布局回归**：`--m11-nav-height` 归 0 后三栏高度需视觉回归确认。
- **R5 best→具体源映射**：popup 调曲线前必经 `resolvedSource`，未解析空态，杜绝拿 `best` 直接打 station/river forecast。
- **R6 display_readonly 瓦片未点亮**：不阻塞前端（GeoJSON 渲染）；以另开 issue 跟踪 424/409。

## Migration / Rollout

按 5 步序列实施（见 tasks.md §1–5），每步独立 `tsc + pnpm test + check:api-types + build` 通过：① 去导航+路由收敛 → ② 就地化+store 改造（最高风险，先 store 单测）→ ③ 代站 cluster 图层 → ④ 两类 popup → ⑤ 删玩具页+迁移+清理。node-27 live receipt 在第 ⑤ 步后产。后端 station-MVT 与 424/409 排查为解耦平行 issue。
