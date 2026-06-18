## Why

展示前端碎成约 10 条路由（`/`、`/overview`、`/basins/:basinId`、`/hydro-met`、`/meteorology`、`/forecast`、`/flood-alerts`、`/segments/:segmentId` …）加一条顶部重导航。其中 `/hydro-met`（2496 行）是 M21 遗留的**不可扩展玩具页**：DOM `<Marker>` 逐点渲染 + 一次性拉全量列表（河段 limit 250、站点 limit 500）+ 搜索/分页/过滤等"很多模块"，与 M11 的瓦片地图（`OverviewPage` 全国总览 / `BasinDetailPage` 流域详情，已跑在 `M11MapLibreSurface` + river-network/hydro MVT 上）严重重复造轮子。

node-27 `display_readonly` 实测已能看到 22 节点交付的 **2 个流域真实产物**：Qhh（386 站 / 1633 河段，GFS+IFS，parsed）、Heihe（1709 站 / 2352 河段，GFS+IFS，frequency_done）。规模在增长（Heihe 已是 QHH 的 4 倍），未来拓展到全国级=**数万代站 / 数百万河段**。当前 DOM-marker + 拉全量的形态根本不可扩展。

需要把展示端收敛成**字面意义上的一张全屏地图 = 整个展示端，无顶部导航**：全国总览与流域详情是同一张图的不同 zoom，气象代站 / 河段流量 / 洪水重现期是图层；架在已有的可扩展瓦片管线（M16 MVT）+ 当前能跑通的 GeoJSON/曲线 API 上；点击地图要素弹出 popup 按需拉取该要素曲线。运维 `/ops` 是 MVP 另一个独立入口，保留但移出主交付。

## What Changes

- **去导航 + 路由收敛**：`AppShell` 删除顶部 `NavBar`；`/`、`/overview`、`/basins/:basinId`、`/hydro-met`、`/meteorology`、`/forecast`、`/flood-alerts`、`/segments/:segmentId` 全部收敛/重定向到单页地图（`replace` + **保留原始 search query** + 附加语义参数，同名键以原始 search 为准）；`/ops`、`/monitoring`、`/system/model-assets`（RBAC 门控）保留可达。
- **总览↔详情就地化**：M11 query 新增 `basinId`，数据 store 的 basinId 改为**从 query 读取**（不再依赖路由 param），单页按 `basinId` 双模式（null=全国总览 / 非 null=流域详情同图 zoom-in），删除 `/basins/:basinId` 路由与 `BasinDetailPage`。
- **气象代站图层（新）**：`M11MapLibreSurface` 新增 clustered-GeoJSON 站点 primitive + 图层切换激活（现 `meteorologyPlaceholders` 占位转为可切换），按选中流域 latest-product 严格身份取 `/api/v1/met/stations`（单次 `limit≤500`，**站点超 500 的流域如 Heihe 1709 站须分页拉取至 client cap 并诚实暴露 `total/loaded/truncated`**）；全国总览无 `basinId` 时显示"选择流域"honest 空态不取数；primitive 为未来 station-MVT 预留 source 抽象。
- **两类地图 popup（新）**：点河段要素→`q_down` 预报曲线 + 洪水重现期三态；点代站→六要素 forcing 曲线（PRCP/TEMP/RH/wind/Rn/Press）。maplibre `Popup` 内嵌 echarts，复用 hydroMet honest-display 校验（严格身份、`ok:false` 不画假曲线、return_period 三态）。
- **删玩具页**：删除 `HydroMetPage`（2496 行）及其专属测试；**保留并迁移** honest-display 库（`bootstrap.ts`/`stationSeries.ts`/`riverForecast.ts`/`ReturnPeriodSection.tsx`）。
- **复用瓦片管线**：河网/q_down/重现期复用 M11 现有 MVT overlay（`/api/v1/layers` metadata 驱动）；图层注册时自动点亮，未注册时如实显示"Layer is not registered"（**不伪造**）。当前 2 流域规模用 M11 既有 GeoJSON 河网渲染（node-27 已验证可取）。
- **不做（本变更明确排除）**：后端 station-MVT 点图层端点（全国万级代站，另开 backend issue）；`display_readonly` 启用 live PostGIS MVT 的根因排查（实测 river-network 瓦片 424 / hydro 瓦片 409，另开 ops/backend issue）；改 DB schema；改 `display_readonly` 后端边界；删 `/ops` 代码；zoom 自动驱动详情（本期先做点选驱动）。

## Capabilities

### New Capabilities

- `single-map-shell-routing`: `AppShell` 去顶部导航 + 旧展示路由收敛/重定向到单页 + 全屏布局（去 `--m11-nav-height`），`/ops`/`/monitoring`/`/system/model-assets` 经 RBAC 直链保留。
- `inplace-overview-basin-detail`: 全国总览↔流域详情就地化——M11 query `basinId` 化 + `overviewData` store 的 basinId 取自 query + 单页双模式 + 删 `BasinDetailPage` 路由，选流域 `fitBounds` zoom-in，可分享可前进后退。
- `met-station-cluster-layer`: 气象代站 clustered-GeoJSON 图层——`M11MapLibreSurface` 新 primitive + 图层切换激活 + 独立 `stationLayerData` store，按选中流域严格身份取数，为未来 station-MVT 预留 source 抽象。
- `map-feature-popups`: 河段 / 代站地图 popup——maplibre `Popup` 内嵌 echarts 曲线，复用 hydroMet honest-display 校验（严格身份、不画假曲线、return_period 三态、honest 空态）。
- `legacy-display-page-retirement`: 删除 `HydroMetPage` 玩具页 + 专属测试，迁移 honest-display 库，旧展示路由重定向，去除拉全量列表/分页/过滤模块。

### Modified Capabilities

<!-- 本变更不修改既有 spec 的需求契约（display_readonly 后端边界、M16 MVT 瓦片契约、strict identity、return-period 可用性口径等保持不变；仅在前端消费侧整合）。 -->

## Impact

- **前端（改/删/新）**：`App.tsx`（路由收敛）、`components/layout/AppShell.tsx`+`NavBar.tsx`（去导航）、`pages/OverviewPage.tsx`（改造为单页、合并 BasinDetail 能力）、`pages/BasinDetailPage.tsx`（**删**）、`pages/hydroMet/HydroMetPage.tsx`（**删**）、`lib/m11/queryState.ts`（`basinId` + `met-stations` layer）、`stores/overviewData.ts`（basinId from query）、`components/map/M11MapLibreSurface.tsx`（cluster primitive + Popup）、`pages/m11/M11Controls.tsx`+`M11Shell.tsx`+`lib/m11/visualTokens.ts`（图层切换 + 全屏高度）、新增 `stores/stationLayerData.ts`、`components/map/M11StationForcingPopup.tsx`、`M11RiverForecastPopup.tsx`、迁移 `ReturnPeriodSection`。
- **保留复用（零开发）**：M11 瓦片管线（river-network/hydro MVT、`/api/v1/layers` 驱动 overlay、`m16-hydrology-mvt-v1`）、M11 河网 GeoJSON primitive（`M11BasinRiverPrimitive`）、hydroMet honest-display 库、`/met/stations`+`/{id}/series`+river-segments+forecast-series 只读 API、`SourceScenarioControls`/`M11Timeline`/`LayerLegendPanel`。
- **测试**：重写 `src/__tests__/AppRoutes.test.tsx`（单页地图模型；react-map-gl mock 补 `Popup`/`Marker`/cluster `getSource`）、扩展 `src/pages/__tests__/M11Shell.test.tsx`、更新 `src/stores/__tests__/overviewData.test.ts`、删 `src/pages/hydroMet/__tests__/ListProduction.test.tsx`、迁移 `ReturnPeriodSection.test.tsx`。
- **不变**：DB schema、`display_readonly` 后端边界（retry/cancel 409、queue 503、no-slurm）、M16 MVT 瓦片契约、honest-display 不变量（不画假曲线、productReady 门控、return_period 三态、strict identity）。
- **验证 oracle**：前端 `tsc`/`pnpm test`/`check:api-types`/`build` 本地；单页地图 + 旧路由重定向 + 代站/河段 popup 的 live receipt 在 **node-27**（`display_readonly`，`/hydro-met`→`/` 重定向、两类 popup 真实拉曲线）。
- **解耦的平行任务（不在本变更，另开 GitHub issue）**：① 后端 station-MVT 点图层矢量瓦片端点（全国数万代站，仿 river-network `ST_AsMVT`，node-22 oracle）；② `display_readonly` 启用/排查 live PostGIS MVT（river-network 瓦片 424 Failed Dependency / hydro 瓦片 409，决定全国态 overlay 能否点亮）。
