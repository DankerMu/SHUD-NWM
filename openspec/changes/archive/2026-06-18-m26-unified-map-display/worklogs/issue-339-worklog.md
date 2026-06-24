---
status: archived
current_authority: "openspec/specs/single-map-shell-routing/spec.md; openspec/specs/legacy-display-page-retirement/spec.md; docs/runbooks/display-readonly-live-mvt.md; docs/runbooks/two-node-deployment-overview.md"
superseded_by: "openspec/specs/single-map-shell-routing/spec.md; openspec/specs/legacy-display-page-retirement/spec.md"
status_since: 2026-06-24
archive_scope: whole-document
retained_for: "audit evidence for M26 met-station cluster layer implementation"
---

# issue-339 worklog — 气象代站 clustered-GeoJSON 图层 (met-station-cluster-layer)

## 角色/oracle/门
同 #337/#338：实现=dispatched fix subagent（leaf 不 commit）；验证=本地四件套；合并门=审核 clean（用户授权不等 CI）。

## 关键架构事实（已勘查）
- `M11Layer` 现 = discharge/water-level/flood-return-period/warning-level（互斥的图层模式，state.layer 单值）。加 `met-stations` 即新增一个互斥图层模式（与 #337 重定向附加的 `layer=met-stations` 对接，#339 后该参数生效）。
- `bootstrap.ts`：`HYDRO_MET_STATION_LIMIT=500`；`fetchHydroMetStations(product, {limit,offset})` 严格身份取自 product（model_id/basin_version_id）；返回 `MetStationPage`（**有 `total_count`/`limit`/`offset`** → 分页/truncation 直接可算）。`loadHydroMetBootstrap({source,cycle,basinId,stationLimit})→{product,stations,stationPage}`。
- `HydroMetSource = 'GFS'|'IFS'`（**不接受 best/compare** → store 需 resolved 源）。resolvedSource 来自 basin detail 的 `sourceSelection.resolvedSource`。
- `M11MapLibreSurface`(1012 行)：primitives（M11BasinPrimitive/M11BasinRiverPrimitive/M11OverlayPrimitive/M11SelectedSegmentPrimitive）；`interactiveLayerIds` 数组（L213）；`onOverlayClick` 经 `findEventFeature` 分发。`<Source ... promoteId>`+`<Layer {...}>` 模式。
- `getHydroMetStationCoordinates(station)→{lon,lat}`（已存在，从 geom）。
- `M11Controls.meteorologyPlaceholders`（L81）占位；`LayerGroupControls`（L198）按 state.layer===item.value 选中。

## 决策
- D-339-1：`queryState` M11Layer 枚举 + layers 数组加 `met-stations`。
- D-339-2：新建 `stores/stationLayerData.ts`：入参 {basinId, resolvedSource:'GFS'|'IFS'}；分页 offset+=500 至 `total_count` 或 client cap（定 5000，国家级守卫；Heihe 1709<cap 故 truncated=false，truncation 路径由 >cap 或测试 mock 触发）；暴露 {stations,total,loaded,truncated}；无 basinId / 源未解析 → 不取数 + honest 空态。不污染 overviewData store。
- D-339-3：`M11StationClusterPrimitive`：`<Source type=geojson cluster clusterRadius=50 clusterMaxZoom=14>` + 3 层（clusters circle / cluster-count symbol / met-stations-point circle）；interactiveLayerIds += met-stations-point,clusters；点 cluster→`getSource().getClusterExpansionZoom`+flyTo（运行时；测试 stub）；以 layerId/source 抽象预留 station-MVT。仅 state.layer==='met-stations' 且有 stations 时渲染。
- D-339-4：页面接线（OverviewPage 双模式）：layer===met-stations 时——detail 模式用该 basin 的 resolvedSource 取数；overview/无 basinId → honest "选择流域" 空态不取数。truncation/total 经 UI 诚实标注。

## 验证矩阵（编排者独立复验）
| 检查 | 状态 |
|---|---|
| tsc | ✅ EXIT=0 |
| vitest | ✅ 645 passed / 30 files（含 3 补测）|
| check:api-types | ✅ EXIT=0 |
| build | ✅ built |

## 候选/裁决 ledger（4 路并行 review，零 critical/major）
| 候选 | reviewer | 裁决 | 处置 |
|---|---|---|---|
| 8 Scenario 全实现 + 测试 | Spec/MetStation | CONFIRMED 正向 | 无 |
| cycle 不进 latest-product 身份查询 | Spec | minor（既有 bootstrap 行为，非本变更回归；latest=最新 cycle）| 记录，不改（跨 bootstrap 共享面）|
| Heihe 真实端点 total/分页需 live 证 | Spec | minor（node-27 oracle 范畴）| → EPIC 收尾 node-27 receipt |
| truncation 已暴露 UI 字段但未写 receipt | Spec | minor（证据采集非代码缺口）| → EPIC 收尾 receipt 捕获 status 文案 |
| 分页四象限正确 + 非 vacuous | Pagination/Identity | CONFIRMED 正向 | 无 |
| 中途页抛错路径无专项测试 | Pagination | minor 覆盖缺口（逻辑正确：throw+data null）| ✅ 已补测 |
| total_count=0/缺失回退无测试 | Pagination | minor 覆盖缺口（逻辑正确）| ✅ 已补测（空流域 + 缺失回退）|
| cluster source/3 层/interactiveLayerIds/点击/分发/MVT 豁免 | Cluster/MapIntegration | CONFIRMED 正向 | 无 |
| 代站点 hover 无 pointer 光标 | Cluster | minor UX | → #340 popup 一并补 |
| 9 条测试全真断言，truncation store+UI 双验，0 skip | Test-Integrity | CONFIRMED 正向 | 无 |

裁决：clean。actionable minor（分页边界测试）已补 3 条闭合（空流域/total_count 缺失回退/中途页抛错）。其余 minor 归位 #340 与 EPIC 收尾 receipt。

## 动态阶段
- [x] Phase 0 评估 + 基线
- [x] Phase 1 fix subagent
- [x] Phase 2 本地验证（4/4 绿）
- [x] Phase 3 commit + PR
- [x] Phase 4-6 review（4 路并行，1 轮 clean）
- [x] Phase 7 独立复核（补 3 条分页边界测试 + 复验 645）
- [x] Phase 8 merge（用户授权，不等 CI）
