# Issue #410 Worklog — [Governance-5 E2-N27-04] Delete legacy old-page components + LegacyPagesHarness

## Roles / oracle
- Orchestrator: scope + 删除 + 本地验证 + node-27 verify + review。
- Verify oracle: node-27 `corepack pnpm test && corepack pnpm build`（tasks §5.3）。
- 依赖 #407✓ #408✓ #409✓（全 merged）—— handoff 生成已迁(/407)、mocked e2e 已迁(/408)、vitest harness 用法已清空(/409)。

## Ground truth (orchestrator scope — 静态分析)
删前 grep 全 src：5 个目标 symbol（ForecastPage/FloodAlertPage/SegmentDetailPage/MeteorologyPage/LegacyPagesHarness）的**唯一外部引用 = `src/__tests__/legacyPagesHarness.tsx`**（它 lazy-import 4 旧页）；`LegacyPagesHarness` 自身**零外部引用**（#409 已清空 107 处用法 + 删 MeteorologyPage.test）。即隔离子图，可整体删除。
实际路径（与最初假设不同）：`src/pages/ForecastPage.tsx`、`FloodAlertPage.tsx`、`SegmentDetailPage.tsx`、`meteorology/MeteorologyPage.tsx`、`__tests__/legacyPagesHarness.tsx`。

## 删除 + 验证
- `git rm` 5 文件。删后 `grep -rnE "ForecastPage|FloodAlertPage|SegmentDetailPage|MeteorologyPage|LegacyPagesHarness|legacyPagesHarness" src` → **NO_REMAINING_REFS**。
- 本地：`tsc --noEmit` exit 0；`pnpm test` 34 files / **557 passed**；`pnpm build` exit 0（✓ built 3.25s）。

## Scope 边界（out-of-scope，不在本 PR 删）
4 旧页曾 import 的共享模块（如 `components/forecast/ForecastPanel`、`components/flood/Alert*`、`lib/meteorology/{contracts,viewModels,queryState}`）删页后**可能**变孤立。但：
- 本 PR scope（tasks 4.4）= 删 5 个命名文件；transitive dead-code 清扫属 `LEGACY_DEAD_CODE_INVENTORY` 单列治理。
- 部分共享组件（ForecastChart/FloodAlertMap 等）单图侧可能仍在用，盲删有回归风险。
- tsc/build 已绿 → 无 dangling ref；孤立模块仅未被引用（build 已 tree-shake），不影响正确性。
→ 作为 out-of-scope artifact 报告（review pack 评估 orphan-completeness 后定 follow-up）。

## Phase state
- [x] Phase 0 scope / [x] 删除+本地验证
- [ ] node-27 verify (`pnpm test && pnpm build`)
- [ ] review panel
- [ ] merge

## Review (1-pack，机械删除·proven-zero-ref) + 裁定
**VERDICT: CLEAN**（0 in-scope CONFIRMED）。
- Dangling ref：删后 src 全域 + e2e 对 5 symbol grep = 0；仅 `openspec/changes/**` 历史 spec 文档有描述性提及（非可执行）。
- 覆盖：`legacyPagesHarness` 删后零 importer；#409 已把测试切到 `render(<App/>)`（AppRoutes.test.tsx import App）。
- scope：实删 commit diff = 仅 5 删 + worklog/tasks（amend 后再移除误带的 `test-results/.last-run.json` + 补 `test-results/`·`playwright-report/` gitignore），无 src 业务码越界。

### Orphan-completeness（report-only，out-of-scope follow-up，**不在本 PR 删**）
删后变「无存活 App 路由可达」的子图：
- **真孤立**：`components/flood/{AlertRankingPanel,AlertStatsPanel,AlertTicker,AlertTimeline,FloodAlertMap,SegmentAlertDetail}` + `stores/floodAlert`（注：`AlertTimeline`/`floodAlert` 同名 type/契约仍被单图 overviewDataContracts/overviewData/api.types 用 → 组件本体孤立、共享 type 必须留）。
- **真孤立**：`lib/meteorology/{contracts,viewModels,queryState}`（仅自环）。
- **疑似孤立**：`components/map/MapView`（仅 AppRoutes.test + 自测 import，无存活 page import）—— 待 dead-code 治理单独核。
- **必须留（单图仍用）**：`components/forecast/{ForecastPanel,SegmentInfo}`、`ScenarioSelector`、`charts/ForecastChart`、`stores/forecast`（活链 OverviewPage→BasinDetailPanels→M11RiverForecastPopup→ForecastChart→forecast store）。
→ 归 `LEGACY_DEAD_CODE_INVENTORY` follow-up issue；与本 PR 删除无 dangling 关联。
- 另记：`AppRoutes.test.tsx:12,214` 有 #409 残留的 flood store/component import（App 不路由 flood）= test cruft，并入 orphan follow-up。

## node-27 live receipt
- HEAD `06affd3`；`corepack pnpm test` → 34 files / **557 passed** (exit 0)；`corepack pnpm build` → ✓ built 16.24s (exit 0)。
- log：`node-27:/tmp/verify-410b-{test,build}.log`。
- 注：首跑 checkout 被 untracked `test-results/.last-run.json` 中止（该生成物被我 `git add -A` 误带入 commit）→ 已 amend 移除 + 补 `test-results/`·`playwright-report/` gitignore；node-27 清 untracked 后重跑通过。

## Phase state（终态）
- [x] scope / [x] 删除+本地验证 / [x] node-27 verify(557 passed+build) / [x] review(CLEAN) / [x] merge gate 满足
