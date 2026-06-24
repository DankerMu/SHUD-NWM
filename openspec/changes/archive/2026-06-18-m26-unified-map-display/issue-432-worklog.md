---
status: archived
current_authority: "openspec/specs/single-map-shell-routing/spec.md; openspec/specs/legacy-display-page-retirement/spec.md; docs/runbooks/display-readonly-live-mvt.md; docs/runbooks/two-node-deployment-overview.md"
superseded_by: "openspec/specs/single-map-shell-routing/spec.md; openspec/specs/legacy-display-page-retirement/spec.md"
status_since: 2026-06-24
archive_scope: whole-document
retained_for: "audit evidence for M26 follow-up dead-code cleanup"
---

# Issue #432 Worklog — 前端死代码清扫（M26 单图收敛遗留孤立模块）

## Roles
- 编排/验证: Claude Code（本地）
- 修复: dispatched fix subagent（编辑 apps/frontend/src，不 commit）
- 评审: dispatched review panel（只读）
- 验证 oracle: 本地 `tsc --noEmit` + `pnpm test` + `pnpm build`（前端验证本地化）；node-27 live receipt（`pnpm test`/`build`）

## 背景
#410 删旧页 + LegacyPagesHarness 后，下列共享模块失去存活 App 路由入口（`docs/governance/LEGACY_DEAD_CODE_INVENTORY.md` Follow-Up #410 条目）。

## 引用图分析（grep-proven，orchestrator 完成）
**确认死（零存活非-flood/非-test 引用）→ 删除：**
- `components/flood/{AlertRankingPanel,AlertStatsPanel,AlertTicker,AlertTimeline,FloodAlertMap,SegmentAlertDetail}.tsx`
- `components/flood/FloodReturnPeriodLayer.tsx` —— ⚠️ 级联孤立：唯一存活引用者是 `FloodAlertMap`（本轮删除）。单图 flood-return-period 渲染走 `alertLevels.floodTileLayerPaint`（live，由 `M11MapLibreSurface` 直接用），React 组件 `FloodReturnPeriodLayer` 已被取代。**超出 #432 字面清单，作为级联清理 + 显式 flag。**
- `stores/floodAlert.ts` —— 仅被死 flood 组件 + AppRoutes.test cruft 引用。单图同名 `AlertTimeline`/`floodAlert` 契约来自**另一文件** `lib/m11/overviewDataContracts.ts`（`ApiFloodAlertTimeline = components['schemas']['FloodAlertTimeline']`，源自生成 types），与本文件无关 → 删除安全。
- `lib/meteorology/{contracts,viewModels,queryState}.ts` —— 三文件自环，零外部引用。
- `components/map/MapView.tsx` —— OverviewPage 渲染 `M11MapLibreSurface`，非 MapView；MapView 仅被 AppRoutes.test mock（inert）+ 自测引用。
- 对应测试文件：`components/flood/__tests__/FloodAlertComponents.test.tsx`、`components/flood/__tests__/FloodReturnPeriodLayer.test.tsx`、`stores/__tests__/floodAlert.test.ts`、`components/map/__tests__/MapView.test.ts`

**必须保留（live）：**
- `components/flood/alertLevels.ts`（被 `overviewDataContracts` + `M11MapLibreSurface` 用）
- `lib/m11/overviewDataContracts.ts`、`stores/overviewData.ts`、单图 forecast popup 链
- AppRoutes.test 中所有单图 `flood-return-period` LAYER 行为断言（@989/991/1088/268-273/302）

**外科手术（AppRoutes.test.tsx）：仅删死机制，留活断言**
- 删 `import { useFloodAlertStore }`（L12）、MapView mock（L56-78）、FloodAlertMap mock（L214-219）、`floodAlertMapProps` 数组（L22）+ beforeEach 中 `.length=0` + `useFloodAlertStore.setState({...})` 块（~L1003）
- 删任何断言 `floodAlertMapProps` 的 `it()` 用例（死 FloodAlertMap 行为）
- 保留单图 flood-return-period layer scope keys / 断言

## 验证
- 本地 `corepack pnpm exec tsc --noEmit` + `corepack pnpm test` + `corepack pnpm build` 全绿
- 删后再 grep 证零存活引用
- node-27 receipt

## 动态阶段状态
- [x] 状态评估 + 引用图（orchestrator）
- [ ] fix subagent 删除 + 本地 tsc/test 自检
- [ ] orchestrator commit/push
- [ ] node-27 receipt
- [ ] cross-review panel → verify gate
- [ ] clean → merge gate

## 候选/裁决账本
- C1 [rev432-complete, minor] `components/map/RiverLayer.tsx` 疑似级联漏删 →
  **CONFIRMED（orchestrator 裁决）**：`git show 1710375^:.../MapView.tsx` 证实被删 MapView
  L20-23 import + L387 渲染 RiverLayer，且 parent commit 中 RiverLayer 唯一引用者就是 MapView
  （其余为同名 `m11BasinRiverLayerColor` 假阳性）。删 MapView 后 RiverLayer 零 live 引用 →
  与 FloodReturnPeriodLayer 同类级联。**补删** `RiverLayer.tsx`（无独立测试、不 import 本地模块、
  导出常量零外部消费者，级联终止）。复验：tsc 0 / test 510 / orphan 终扫零悬空。
- C2 [rev432-tests] CLEAN — 仅删死机制，单图 flood-return-period 断言完整，覆盖无损。
- C3 [rev432-scope] CLEAN — FloodReturnPeriodLayer 级联 JUSTIFIED，meteorology/MapView 删除无回归，无越权。

## 决策
- D1: FloodReturnPeriodLayer 级联删除（超 #432 字面清单），显式 flag；3 评审一致 JUSTIFIED；trivial revert 可回退。
- D2: RiverLayer.tsx 级联补删（review 发现，CONFIRMED）——MapView 唯一子组件，同属删除的旧地图栈。
