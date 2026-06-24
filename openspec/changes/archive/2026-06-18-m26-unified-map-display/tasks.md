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
retained_for: "audit evidence for M26 display-route verification"
---

## 0. 前置与基线

- [x] 0.1 用 `openspec validate m26-unified-map-display --strict --no-interactive` 校验本 change 4/4 complete 后再开实现。
- [x] 0.2 记录基线：现有路由表（`App.tsx`）、`AppShell`/`NavBar` 导航项、`M11QueryState` 字段、`overviewData` store 的 `loadOverview`/`loadBasinDetail` 签名与 snapshot 匹配函数，作为回归基准。
- [x] 0.3 清点 honest-display 库的导出与消费点（`bootstrap.ts`/`stationSeries.ts`/`riverForecast.ts`/`ReturnPeriodSection.tsx`），列出迁移后需保留的导出名（防测试大面积改 import）。
- [x] 0.4 记录 node-27 实测约束作为实现依据：`best` 不被 latest-product 接受（仅 GFS/IFS）、`/api/v1/layers` 空、river-network 瓦片 424 / hydro 瓦片 409（→ 当前用 GeoJSON 河网渲染，overlay 待注册自动点亮）。【2026-06-08 更新（issue #343 收尾）：已置 `NHMS_ENABLE_LIVE_POSTGIS_MVT=true`，`/api/v1/layers`=5 图层、hydro-national 瓦片 200/370KB live PostGIS MVT 已点亮；根因+决策+receipt 见 `docs/runbooks/display-readonly-live-mvt.md`。】

## 1. 去导航 + 路由收敛（single-map-shell-routing）

- [x] 1.1 `components/layout/AppShell.tsx` 删除 `<NavBar/>` 及 import；保留 `ToastProvider`/`<main>`/role override；可在角落保留 operator+ 可见的低调"运维"直链。
- [x] 1.2 `lib/m11/visualTokens.ts` 的 `--m11-nav-height` 去 nav 后归 0；`pages/m11/M11Shell.tsx`（`M11Layout`）高度改为全视口，验证三栏不塌陷/留白。
- [x] 1.3 `App.tsx`：`/`→`DisplayMapPage`；`/overview`/`/forecast`/`/hydro-met`→`<Navigate replace>` 到 `/`；`/meteorology`→附加 `layer=met-stations`；`/flood-alerts`→附加 `layer=flood-return-period`；`/basins/:basinId`→附加 `basinId=:basinId`；`/segments/:segmentId`→附加 `segmentId=:segmentId`；`/ops`/`/monitoring`/`/system/model-assets` 不动。重定向 MUST 保留原始 search（读 param+search 合成），同名键以原始 search 为准；`/segments/:segmentId` 缺 basin 上下文时落 `/?segmentId=…` 由单页 honest 空态处理。
- [x] 1.4 测试：`AppRoutes.test.tsx` 增/改各旧路由（含 `/segments/:segmentId`）重定向落点断言 + 深链原始 search 保留断言 + `/` 渲染单页 + `/ops` 等仍受 RBAC（不被重定向）。

## 2. 总览↔详情就地化 + 数据 store 改造（inplace-overview-basin-detail）【最高风险，先做 store 单测】

- [x] 2.1 `lib/m11/queryState.ts`：`M11QueryState` 新增 `basinId: string | null`，纳入 `parseM11QueryState`/`serializeM11QueryState`/默认值/identifier 白名单校验。
- [x] 2.2 `stores/overviewData.ts`：`loadBasinDetail` 的 basinId 入口由路由 param 改为 query；snapshot 匹配函数按 query 内 basinId 判定；store 内部取数键与 honest 契约不变。**先补 store 单测覆盖 basinId-from-query**（护栏），再改页面。
- [x] 2.3 `pages/OverviewPage.tsx` 改造为 `DisplayMapPage`：按 `state.basinId` 双模式（null=总览 / 非 null=详情同图）；选 basin（点 `m11-basin-fill`/可见性树"进入分析"）→ `handleQueryChange({ basinId })`+`fitTo` bbox；"返回总览"→ `handleQueryChange({ basinId: null, segmentId: null })`。
- [x] 2.4 将 `BasinDetailPage` 的详情面板（`SegmentDiscoveryPanel`/`SelectedSegmentPanel`/趋势/对比/RP 三态）抽到 `components/m11/BasinDetailPanels.tsx` 并入单页；删除 `pages/BasinDetailPage.tsx` 与其路由。
- [x] 2.5 测试：`overviewData` 测试更新（basinId from query）；`AppRoutes.test.tsx` 加 basinId 切换驱动 `loadBasinDetail`+`m11FitBoundsCalls` 命中、河段列表出现、`basinId=null` 回总览；`M11Shell.test.tsx` 基本不动。

## 3. 气象代站 clustered-GeoJSON 图层（met-station-cluster-layer）

- [x] 3.1 `lib/m11/queryState.ts` 新增 `M11Layer` 值 `met-stations`；`pages/m11/M11Controls.tsx` 把 `meteorologyPlaceholders` 中代站项转为可切换图层。
- [x] 3.2 新建 `stores/stationLayerData.ts`：薄 store 复用 `loadHydroMetBootstrap`→`fetchHydroMetStations`，按选中流域 latest-product 严格身份取数；单次 `limit≤500`，**站点超 500 的流域（Heihe 1709）分页（offset）拉取至明确 client cap，暴露 `total/loaded/truncated`** 供 UI/receipt 诚实标注；源为 best/compare 先经 `resolvedSource` 落 GFS/IFS，未解析不取数；**全国总览无 basinId 时不取数、显示"选择流域"honest 空态**。
- [x] 3.3 `components/map/M11MapLibreSurface.tsx` 新增 `M11StationClusterPrimitive`：`<Source type="geojson" cluster clusterRadius clusterMaxZoom>` + `clusters`/`cluster-count`/`met-stations-point` 三层；`interactiveLayerIds` 纳入 point/clusters；点 cluster→`getSource().getClusterExpansionZoom`+`flyTo`（运行时，测试 stub）；以 `layerId`/`source` 抽象预留 station-MVT 切换。
- [x] 3.4 测试：`M11Shell.test.tsx` 扩展 cluster source/layer 注册断言、关闭不渲染、未解析源不取数、超 500 流域 truncation 标注、无 basinId honest 空态；`AppRoutes.test.tsx` 加 met-stations toggle。

## 4. 两类地图 popup（map-feature-popups）

- [x] 4.1 `ReturnPeriodSection.tsx` 从 `pages/hydroMet/` 迁到 `components/m11/`（保留导出名）；`ReturnPeriodSection.test.tsx` 更新 import 路径。
- [x] 4.2 新建 `components/map/M11RiverForecastPopup.tsx`：点河段→按 `river_segment_id` 调 `loadHydroMetRiverForecast`+`validateHydroMetRiverForecastForChart`→`ForecastChart`(q_down)+`ReturnPeriodSection`(三态)；`ok:false` 显原因空态不画曲线。
- [x] 4.3 新建 `components/map/M11StationForcingPopup.tsx`：点代站→按 `station_id` 调 `loadHydroMetStationSeries`+`validateHydroMetStationSeriesIdentity`→六要素 echarts；身份不符空态。popup 经纬度定位 + source 用 `resolvedSource`，best 未解析显空态。
- [x] 4.4 单页挂载两类 popup（`M11Layout` children 模式）；点击经 `onOverlayClick` 分发要素到对应 popup。
- [x] 4.5 测试：新增 popup 单测（正常曲线 / `ok:false` 不画曲线 / 身份不符空态 / productReady 门控 / best 未解析空态）；`AppRoutes.test.tsx` 集成点河段/代站触发 popup；`react-map-gl/maplibre` mock 补 `Popup`/`Marker`/`getSource`(cluster) 导出 + 注入 `met-stations-point`/河段 feature 的 onClick 分支。

## 5. 删玩具页 + 清理（legacy-display-page-retirement）

- [x] 5.1 删除 `pages/hydroMet/HydroMetPage.tsx` 与 `pages/hydroMet/__tests__/ListProduction.test.tsx`；移除对 `ReadyHydroMetContent` 的全部 import。
- [x] 5.2 删除/精简 `pages/hydroMet/BasinSelector.tsx`（如不再复用）；确认 `bootstrap.ts`/`stationSeries.ts`/`riverForecast.ts` 保留且被 popup 复用。
- [x] 5.3 `NavBar.tsx` 删除或精简为未用（如运维直链复用则保留精简版）；`MeteorologyPage`/`SegmentDetailPage` 暂保留（仅其展示路由重定向，文件不在删除范围）。
- [x] 5.4 全仓 grep 确认无悬挂 import / 死路由 / 残留 testid 引用；`pnpm build` 无未用导出告警阻断。

## 6. 本地验证 + node-27 live receipt

- [x] 6.1 每步与终态本地全绿：`cd apps/frontend && corepack pnpm exec tsc --noEmit && corepack pnpm test && corepack pnpm run check:api-types && corepack pnpm build`。
- [x] 6.2 本地/github/node-27 三端同步：commit → push → node-27 `git pull --ff-only`（先 `git status --porcelain` 把关，绝不 stash pop）→ 在 node-27 重建 `apps/frontend/dist`。
- [x] 6.3 node-27 live receipt（`display_readonly`，走隧道 `--headed` 验真实地图渲染，无头无 WebGL）：① `/hydro-met`/`/overview`/`/forecast`/`/meteorology`/`/flood-alerts`/`/basins/:id`/`/segments/:id` 重定向到 `/` 单页（语义参数 + 原始 search 保留）；② 全屏地图无顶部导航；③ basinId 切换 QHH↔Heihe 同页 zoom-in；④ 气象代站图层 toggle + 点代站 popup 出六要素真实曲线；⑤ 点河段 popup 出 q_down 真实曲线 + 重现期状态（QHH unavailable / Heihe ready）；⑥ overlay 未注册时如实显示"未注册"不伪造。 ——live-PASS ①②③⑥+display_readonly 身份+数据就绪；④⑤ popup 绘制由本地单测全覆盖+数据 live 就绪，live 点击因 /api/v1/basins 无 bbox+CLI canvas 限制延后→#343（见 worklogs/node27-live-receipt.md）
- [x] 6.4 记录 receipt 到 `openspec/changes/m26-unified-map-display/worklogs/`（含截图路径、重定向矩阵、两类 popup 真实拉曲线证据、424/409 现状说明）。

## 7. 文档与解耦平行 issue

- [x] 7.1 完成的 task 即时勾选；PR body 含变更摘要 + 测试证据 + node-27 receipt 覆盖声明。
- [x] 7.2 另开 backend issue：后端 station-MVT 点图层矢量瓦片端点（全国数万代站，仿 river-network `ST_AsMVT`，node-22 oracle）——记录依赖与预期 PR 边界，不在本变更实现。
- [x] 7.3 另开 ops/backend issue：`display_readonly` 启用/排查 live PostGIS MVT（river-network 瓦片 424 / hydro 瓦片 409，决定全国态 overlay 能否点亮）。
