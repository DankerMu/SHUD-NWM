# Issue #409 Worklog — [Governance-5 E2-N27-03] Migrate Vitest coverage away from LegacyPagesHarness

## Roles / oracle
- Orchestrator: Claude Code（scope + 派发 fix subagent + node-27 verify + review）。
- fix subagent：执行测试迁移（leaf，不 commit）。
- Verify oracle: node-27 `corepack pnpm test && corepack pnpm build`。
- 依赖 #407✓（merged）。

## Ground truth (orchestrator scope)
- `LegacyPagesHarness`（`src/__tests__/legacyPagesHarness.tsx`）挂载旧页 `/forecast`=ForecastPage、`/flood-alerts`=FloodAlertPage、`/meteorology`=MeteorologyPage、`/segments/:id`=SegmentDetailPage + `/`=OverviewPage + `/monitoring`/`/ops`=MonitoringPage + `/system/model-assets`=ModelAssetsPage。
- 用法：`AppRoutes.test.tsx` 107 处 `render(<LegacyPagesHarness/>)`（共 123 it）；`MeteorologyPage.test.tsx` 直接 import MeteorologyPage。
- 关键：`<App/>` 对 `/`、`/overview`、`/monitoring`、`/ops`、`/system/model-assets` 挂载**同一组件**（与 harness 一致）；仅旧页路由 `<App/>` 走 LegacyRedirect→`/`、harness 挂旧页 = 唯一差异。
- 单图侧已有重度独立覆盖：M11Shell.test、overviewData.test、M11RiverForecastPopup/StationForcingPopup、floodAlert.test、FloodReturnPeriodLayer、ReturnPeriodSection、stationSeries/riverForecast libs。
- #410 将删旧页 + LegacyPagesHarness → #409 必须清空 harness 用法 + 旧页 import（含 MeteorologyPage.test.tsx 旧页 import）。

## Phase state
- [x] Phase 0 scope（本文件）
- [ ] Phase 1 fix subagent 迁移
- [ ] node-27 verify
- [ ] review panel（重点查覆盖丢失）
- [ ] merge

## Phase 1 实现 (fix subagent) — 完成
改动：
- `AppRoutes.test.tsx`：移除 LegacyPagesHarness import；**65 处 App-同构测试** `render(<LegacyPagesHarness/>)`→`render(<App/>)`（`/`、`/overview`、`/monitoring`、`/ops`、`/ops/`、`/system/model-assets` 及停留 `/` 的单图 basin/popup/overview）；**删 41 个旧页 deep 行为 it-block**（ForecastPage/SegmentDetailPage/FloodAlertPage/MeteorologyPage，生产已 redirect 孤立）；清死辅助（mockSegmentDetailRouteClient 等）；1 处断言适配（invalid overview query 归一落点 `/overview`→`/`，仍校验"只归一一次"不变量）。
- 删 `pages/meteorology/__tests__/MeteorologyPage.test.tsx`（唯一直接 import 旧页者）。
- 未动 App.tsx / legacyPagesHarness.tsx（留 #410）/ 旧页组件 / e2e。

删除测试 → 单图覆盖映射（不变量未丢）：
- ForecastPage(8) → `M11RiverForecastPopup.test.tsx`（ok:false 无曲线、identity 拒、source/cycle、KPI、空态）+ `riverForecast.test.ts`（segment-id/非有限/非法日期拒、q_down 身份、shorter-horizon 诚实）。
- SegmentDetailPage(20) → 上 + `M11StationForcingPopup.test.tsx` + `stationSeries.test.ts` + `ReturnPeriodSection.test.tsx`（频率/阈值/over-budget/station forcing 行/受限/缺失/截断）。
- FloodAlertPage(12) → `FloodReturnPeriodLayer.test.tsx` + `floodAlert.test.ts` + `FloodAlertComponents.test.tsx`（degraded bbox、centroid 缺失阻断、run/sibling identity 拒、IFS handoff 清旧、warning level/ranking/timeline）。
- MeteorologyPage(2+文件) → `stationLayerData.test.ts`（分页/cap/截断/identity）+ `M11StationForcingPopup.test.tsx` + AppRoutes 现存 station popup 用例（已 `<App/>`）。

本地复核（orchestrator 亲跑）：tsc 0 error；`pnpm test` 34 files / 557 passed；`pnpm build` ✓。
harness import 清零（仅 legacyPagesHarness.tsx:37 自身定义）；旧页 import NONE。
