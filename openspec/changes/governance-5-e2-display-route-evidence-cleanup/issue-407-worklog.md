# Issue #407 Worklog — [Governance-5 E2-N27-01] Migrate old display URL handoff to single-map query form

## Roles / oracle
- Orchestrator: Claude Code（本地编辑 + 本地 vitest/build 快验）。
- Verify oracle: **node-27**（display_readonly），acceptance `cd apps/frontend && corepack pnpm test && corepack pnpm build` 实机 receipt。
- 依赖 #404✓（merged）。

## Scope analysis (ground truth)
- handoff URL 构造器 `m11QueryHref(pathname, state, patch)` = `${pathname}?${serialized}`（`lib/m11/queryState.ts`）。
- **活跃单图（`/` OverviewPage）唯一的旧路由 handoff 生成**：`lib/m11/overviewDataContracts.ts:795` `handoffUrl: m11QueryHref('/forecast', {... segmentId ...})` —— 被 overviewData store 的 `selectedSegment.handoffUrl` 消费。
- 单图 `/` 经 `parseM11QueryState` 消费 `segmentId`/`basinId`/`layer`（queryState.ts:144），故 `/?…&segmentId=X` 可在单图打开该河段 —— 产品行为允许迁移。
- App.tsx 仅 lazy-import OverviewPage/Monitoring/ModelAssets；ForecastPage/SegmentDetailPage/MeteorologyPage/FloodAlertPage **无人 import = 全孤立**（旧路由全 `LegacyRedirect`）。其内的 `/segments`/`/basins`/`/forecast`/SegmentAlertDetail handoff 是死代码 → **#410 删除范畴，非 #407**。

## Change
- `apps/frontend/src/lib/m11/overviewDataContracts.ts`：`m11QueryHref('/forecast', …)` → `m11QueryHref('/', …)`（保留全部 query 参数，含 segmentId/layer/cycle/validTime）。
- focused URL-生成单测断言 `/forecast?` → `/?`：`m11OverviewDataContracts.test.ts`(×2)、`overviewData.test.ts`(×3)。
- 不动 App.tsx redirect alias（保留兼容）；不删旧页（#410）；不改 mocked Playwright/LegacyPagesHarness（#408/#409）。

## Verification
- 本地：`corepack pnpm test` → 35 files / 637 passed；`corepack pnpm build` → built ✓；受影响 2 文件 79/79。
- node-27 live receipt：待 push 后实机 `pnpm test && pnpm build`。

## Phase state
- [x] Phase 0 评估（#404 merged，前置满足）
- [x] Phase 1 实现 + 本地 verify
- [ ] node-27 live verify（oracle）
- [ ] review panel
- [ ] merge：CI green → 自动 merge
