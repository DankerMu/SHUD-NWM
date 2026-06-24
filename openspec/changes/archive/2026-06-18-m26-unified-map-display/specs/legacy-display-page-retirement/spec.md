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
retained_for: "audit evidence for the archived M26 legacy display page retirement delta"
---

## ADDED Requirements

### Requirement: 删除 HydroMetPage 玩具页并迁移 honest-display 库

`apps/frontend/src/pages/hydroMet/HydroMetPage.tsx`（DOM marker + 拉全量列表 + 搜索/分页/stream_order 过滤/变量·QC 筛选）SHALL 被删除，其专属测试（`__tests__/ListProduction.test.tsx`）一并删除。honest-display 库 `bootstrap.ts`、`lib/hydroMet/stationSeries.ts`、`lib/hydroMet/riverForecast.ts`、`ReturnPeriodSection.tsx` MUST 保留并迁移/复用于单页 popup（迁移时保留导出名以减小测试改动）。

#### Scenario: 玩具页与其测试被删除
- **WHEN** 检索仓库
- **THEN** 不存在 `HydroMetPage.tsx` 与 `ListProduction.test.tsx`，且无对 `ReadyHydroMetContent` 的 import

#### Scenario: honest-display 库保留可用
- **WHEN** 单页 popup 渲染河段/代站曲线
- **THEN** 复用 `loadHydroMetRiverForecast`/`loadHydroMetStationSeries`/`ReturnPeriodSection` 等保留库，其严格身份与不画假曲线逻辑不变

### Requirement: 去除拉全量列表/分页/过滤模块

单页 MUST NOT 保留原 `/hydro-met` 的"站点列表 + 河段列表 + 搜索 + 分页 + stream_order 过滤 + 变量/QC 筛选"模块；要素发现改由地图点选（图层 + 点击 popup）承载。删除这些模块对应的不再适用测试。

#### Scenario: 列表/分页模块不再存在
- **WHEN** 渲染单页地图
- **THEN** 不渲染原 hydro-met 的列表/分页/过滤模块，相关 testid（如 `hydro-met-river-pagination`、`hydro-met-river-stream-order-filter`）不再出现

### Requirement: AppRoutes 与受影响测试更新为单页地图模型

`src/__tests__/AppRoutes.test.tsx` SHALL 重写为单页地图模型（旧路由重定向、图层切换、河段/代站点击 popup），其 `react-map-gl/maplibre` mock MUST 补齐 `Popup`、`Marker`、cluster 相关（`getSource`→`getClusterExpansionZoom` stub）导出。`ReturnPeriodSection.test.tsx` 随迁移更新 import；`M11Shell.test.tsx`、`overviewData` 测试按改造同步。

#### Scenario: AppRoutes 测试覆盖单页模型
- **WHEN** 运行 `AppRoutes.test.tsx`
- **THEN** 用例断言 `/` 单页、旧路由重定向落点、图层 toggle、点河段/代站触发数据，且不再引用已删玩具页

#### Scenario: react-map-gl mock 支撑 popup/cluster
- **WHEN** 测试点击河段/代站要素
- **THEN** mock 的 `Popup`/`Marker` 与 `getSource` stub 使 popup 与 cluster 交互可断言（不依赖真实 WebGL）
