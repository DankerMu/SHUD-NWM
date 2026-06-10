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
