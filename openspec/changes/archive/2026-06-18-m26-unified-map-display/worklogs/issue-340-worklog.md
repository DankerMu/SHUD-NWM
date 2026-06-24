---
status: archived
current_authority: "openspec/specs/single-map-shell-routing/spec.md; openspec/specs/legacy-display-page-retirement/spec.md; docs/runbooks/display-readonly-live-mvt.md; docs/runbooks/two-node-deployment-overview.md"
superseded_by: "openspec/specs/single-map-shell-routing/spec.md; openspec/specs/legacy-display-page-retirement/spec.md"
status_since: 2026-06-24
archive_scope: whole-document
retained_for: "audit evidence for M26 map feature popup implementation"
---

# issue-340 worklog — 河段/代站两类地图 popup (map-feature-popups)

## 角色/oracle/门：同前。实现=fix subagent；验证=本地四件套；合并门=审核 clean（不等 CI）。

## 关键事实（已勘查）
- honest-display 库（保留复用，#341 才删 HydroMetPage 本身）：
  - `lib/hydroMet/riverForecast.ts`：`loadHydroMetRiverForecast({product:HydroMetRiverForecastProductIdentity, segment:HydroMetRiverForecastSegmentIdentity})`；`validateHydroMetRiverForecastForChart(...)→{ok:true,...renderedPoints/horizonLabel/...}|{ok:false,messages}`；`hydroMetRiverScenarioForSource`；变量 `q_down`。
  - `lib/hydroMet/stationSeries.ts`：`loadHydroMetStationSeries({product:HydroMetStationSeriesProductIdentity, stationId})`；`validateHydroMetStationSeriesIdentity(...)`；六要素 `['PRCP','TEMP','RH','wind','Rn','Press']`。
  - `pages/hydroMet/ReturnPeriodSection.tsx`：导出 `ReturnPeriodSection`/`ReturnPeriodLegend`/`ProductStatusBar`/`RETURN_PERIOD_LEGEND`/`RETURN_PERIOD_RESULT_UNAVAILABLE`（迁到 components/m11，保留导出名）。
- `components/charts/ForecastChart.tsx`（q_down 曲线）。
- **HydroMetPage.tsx 是现成参考实现**：它如何从 QhhLatestProduct + segment/station 构造上述 identity、调 load+validate、渲染 ForecastChart/六要素 echarts、ok:false 不画曲线、身份不符空态——popup 直接照搬这套逻辑（#341 才删 HydroMetPage）。
- **product 来源**：QhhLatestProduct 由 `loadHydroMetBootstrap({source:GFS/IFS, cycle, basinId})` 解析（#339 stationLayerData 已用）。river popup 在 discharge 模式也要能用 → 需独立于 station 图层拿到 product。
- **react-map-gl `Popup` 必须是 `<Map>` 子元素** → popup 由 `M11MapLibreSurface`（持有 `<Map>`）渲染，内容/选中要素由页面经 props 提供。
- onOverlayClick 现分发：river segment→`layerId:'basin-river-segments'`；station→`layerId:'met-stations'`（#339 预留，feature 带 station_id）。

## 决策
- D-340-1：`git mv` ReturnPeriodSection 到 components/m11，保留导出名；更新所有 import（ReturnPeriodSection.test.tsx + HydroMetPage 现存 import + 可能 ListProduction.test）。
- D-340-2：新建共享 hook `useHydroMetProduct(basinId, resolvedSource:GFS/IFS, cycle)`（复用 loadHydroMetBootstrap，带缓存），页面解析 product 传给两 popup；best 未解析/无 basin → product null → popup 空态。
- D-340-3：`M11RiverForecastPopup`（点河段→按 river_segment_id 构造 segment identity + product → loadHydroMetRiverForecast+validate→ForecastChart(q_down)+ReturnPeriodSection 三态；ok:false 显原因不画曲线）。
- D-340-4：`M11StationForcingPopup`（点代站→按 station_id + product → loadHydroMetStationSeries+validateIdentity→六要素 echarts；身份不符空态；best 未解析空态）。
- D-340-5：popup 由 M11MapLibreSurface 内 `<Popup longitude latitude onClose>` 渲染（river 用 event lngLat / 选中 segment geometry 锚点；station 用 station 坐标）；页面经 onOverlayClick 设选中要素 + 传 popup 内容。
- D-340-6：测试 + react-map-gl mock 补 `Popup`/`Marker`。

## 验证矩阵
| 检查 | round-1 | round-2(fix 后) |
|---|---|---|
| tsc/vitest/check:api-types/build | ✅ 659 | ✅ 673 / 34 files |

### Round 2（fix 后 3 路 comprehensive review）— CLEAN
| 候选 | reviewer | 裁决 |
|---|---|---|
| station popup 红线闭合（同源复用金标准，无宽松分支；缺 unit/坏 metadata/malformed 全 ok:false 不画）| HonestDisplay-r2 | **CONFIRMED 闭合，None** |
| 抽取字节级等价（10/11 identical，1 处纯 TS cast 运行时不变）+ HydroMetPage 零行为变化 + 673 无回归 | Extraction/Regression-r2 | None critical/major（1 minor: TS cast 无害）|
| 4 条 popup 护栏 + 10 条 lib 直测硬断言 echarts 缺失、mock 忠实、0 skip | Test-Guard-r2 | **None** |

裁决：round-2 clean。红线违规闭合且有真实护栏。fix = 金标准抽到 lib 共享（DRY，#341 删 HydroMetPage 后 popup 仍持有校验器）。

## 候选/裁决 ledger
### Round 1（4 路并行 review）— NOT clean（2 critical + 1 major）
| 候选 | reviewer | 裁决 | 处置 |
|---|---|---|---|
| **station popup 内联 parsePoints 比金标准更宽松：malformed/NaN 点静默丢弃后画 survivors（金标准 = 任一无效点→ok:false→空态）** | HonestDisplay | **CONFIRMED critical** | fix 轮：抽金标准校验到 lib 共享 |
| **station popup 无 metadata/unit 长度/truncated/QC 契约校验即绘图** | HonestDisplay | **CONFIRMED critical** | 同上 |
| station popup 缺 unit 仍画（无 unit 门控） | HonestDisplay | **CONFIRMED major** | 同上 |
| station popup truncation/cap/QC 无披露 | HonestDisplay | minor | 随 fix 一并（复用金标准免费带） |
| river ok:false 不画曲线 / 身份不符空态 / product=null 空态 / RP 三态 / 消息 sanitize | HonestDisplay | 正向 CONFIRMED | 无 |
| 8 Scenario 实现+测 | Spec/Popups | 正向 | 无 |
| RP 测试用例注释名实不符 | Spec/Popups | minor | 随 fix 顺手 |
| 清理 effect bug 修复正确（ref 记上一具体源）| Integration/BugFix | CONFIRMED 正向 | 无 |
| ReturnPeriodSection git mv 100% 无回归 | Integration | 正向 | 无 |
| mock 不掩盖 no-fake-curve（echarts stub 反映点数）| Test-Integrity | 正向 | 无 |
| **station popup 无 malformed/missing-unit/bad-metadata 测试**（critical 路径无护栏）| HonestDisplay+Test-Integrity | 测试缺口 | fix 轮补测 |

裁决：**NOT clean**。fix：抽 HydroMetPage 的 `validateHydroMetStationSeriesForChart`/`parseChartableStationSeriesPoint`（含 unit/metadata/QC/truncation/reject-on-any-invalid）到 `lib/hydroMet/stationSeries.ts`，HydroMetPage 与 popup **同源复用**；补 malformed/缺 unit/坏 metadata/truncation 测试。fix 后重跑全 4 路 comprehensive review。

## 动态阶段
- [x] Phase 0 评估 + 基线
- [x] Phase 1 fix subagent（round-1，有 honest-display 违规）
- [x] Phase 4-6 round-1 review（NOT clean：2 critical+1 major）
- [x] Phase 6 fix round-1（抽金标准校验到 lib/hydroMet/stationSeries.ts，HydroMetPage+popup 同源复用）
- [x] Phase 4-6 round-2 comprehensive review（3 路 None，clean）
- [x] Phase 7 独立复核（编排者直读 round-2 evidence：字节等价 + echarts 缺失硬断言 + 673 无回归）
- [x] Phase 8 merge（PR #347 squash-merge 8b7befc，用户授权不等 CI；#340 已关闭，tasks.md §4 已勾）
