---
status: archived
current_authority: "openspec/specs/single-map-shell-routing/spec.md; openspec/specs/legacy-display-page-retirement/spec.md; docs/runbooks/display-readonly-live-mvt.md; docs/runbooks/two-node-deployment-overview.md"
superseded_by: "openspec/specs/single-map-shell-routing/spec.md; openspec/specs/legacy-display-page-retirement/spec.md"
status_since: 2026-06-24
archive_scope: whole-document
retained_for: "audit evidence for M26 single-map shell routing implementation"
---

# issue-337 worklog — 去导航 + 路由收敛到单页 (single-map-shell-routing)

## 角色
- 编排/验证：Claude Code（本地）
- 实现：dispatched fix subagent（leaf，不 commit）
- 验证 oracle：本地 `tsc`/`vitest`/`check:api-types`/`build`（纯前端路由/外壳，不涉 node-22 DB / node-27 live）
- 合并门（用户授权）：审核 clean 即 merge，不等 CI；EPIC 全绿后统一处理 CI

## 关键基线（实现前）
- `App.tsx`(78 行)：`/`+`/overview`→OverviewPage；`/basins/:basinId`→BasinDetailPage；`/hydro-met`→HydroMetPage；`/meteorology`/`/forecast`/`/flood-alerts`/`/segments/:segmentId`→各页；`/monitoring`/`/ops`/`/system/model-assets` RBACGate。全包在 `<AppShell>`。
- `AppShell.tsx`：sticky header(NHMS brand + `<NavBar/>` + role-override Select) + `<main>` + ToastProvider。
- **`NavBar.tsx` 的 `useEffect` 承载全局 `fetchRuntimeConfig()`**（display_readonly 检测的唯一加载点）——删 NavBar 必须迁走。
- `visualTokens.ts`：`navHeight:'56px'`；`M11Shell.tsx:70` 用 `h-[calc(100vh-var(--m11-nav-height)-32px)]`，:87 设 `--m11-nav-height: m11VisualTokens.navHeight`。
- `AppRoutes.test.tsx` 8563 行单 describe，旧多页模型；`/hydro-met`(~40)、`/basins/`、`/segments/`、`/meteorology`、`/forecast` 深度行为测试在内。
- honest-display 不变量(`stationSeries`/`riverForecast` 的 ok:false 不画曲线 / 身份校验)**仅** AppRoutes.test.tsx 覆盖（`bootstrap.test.ts` 只覆盖 bootstrap）→ 重定向不能直接删测试，否则丢不变量覆盖。
- 被重定向页有独立测试：MeteorologyPage.test.tsx、ForecastComparison.test.tsx、FloodAlertComponents.test.tsx、floodAlert.test.ts、overviewData.test.ts。

## 决策
- D-337-1：`/hydro-met` 按 spec 在本 issue 重定向（不偏离 spec）。
- D-337-2：删 NavBar，**runtime-config fetch 迁入 AppShell**（保留 display_readonly 加载）；去 sticky header 导航，保留 role-override（浮层）+ ToastProvider；`navHeight→0px`。
- D-337-3：测试用 **LegacyPagesHarness**（测试内旧路由表，直挂页面组件）承载深度行为测试以零覆盖损失；真 `<App/>` 仅跑重定向矩阵 + RBAC。页面在后续 issue 删除时连带删 harness 条目（#338 BasinDetail、#341 HydroMet）。
- D-337-4：NavBar.tsx 文件本 issue 删除（迁走 fetch 后无其他引用），避免孤儿。

## 验证矩阵（编排者独立复验）
| 检查 | 命令 | 状态 |
|---|---|---|
| 类型 | `corepack pnpm exec tsc --noEmit` | ✅ EXIT=0 |
| 单测 | `corepack pnpm test` | ✅ 620 passed / 29 files |
| API 类型 | `corepack pnpm run check:api-types` | ✅ EXIT=0 diff 无差异 |
| 构建 | `corepack pnpm build` | ✅ built |

## 候选/裁决 ledger（3 路并行 review，零 critical/major）
| 候选 | reviewer | 裁决 | 处置 |
|---|---|---|---|
| LegacyRedirect 不对 path param URL 编码 | Spec/Routing | REFUTED（URLSearchParams.toString 自动编码,非 bug） | 无 |
| replace 不污染回退栈测试依赖 jsdom history.length 偏弱 | Spec/Routing | PLAUSIBLE-非阻断（实现用 replace 正确） | 记录,不改 |
| segment 缺 basin 的 honest 空态未断言 | Spec/Routing | 按设计（属 #338/#339 页面层） | #338 关闭 |
| 零覆盖损失 | Test-Integrity | CONFIRMED 正向（it 140→150、hydro-met 不变量 14==14、无 skip） | 无 |
| PR/worklog 把 segmentId 误列"被清掉" | Integration | CONFIRMED（segmentId 是 M11QueryState 正式字段会保留） | ✅ 已修 PR body + 本 worklog |
| 去导航致深链功能降级 | Integration | 按设计（#336 收敛预期,无崩溃,build/vitest 绿） | #338/#339 关闭 |
| role-override 浮层与右面板折叠按钮潜在共位 | Integration | PLAUSIBLE-非阻断（仅 dev 态） | #338 重排布局避让 |

裁决：clean（0 in-scope CONFIRMED 代码缺陷，0 merge-blocking）。唯一 actionable = PR body segmentId 措辞（doc-only，已修，不触发重审）。

## 已知限制更正（Integration reviewer 纠正）
被 OverviewPage 归一化清掉的只有 **`basinId`**（M11QueryState 无此字段，#338 加）与 **`layer=met-stations`**（不在 layers 白名单 queryState.ts:22，parse 回退 discharge，#339 加）。**`segmentId` 是 M11QueryState 正式字段（queryState.ts:14），serialize 原样回写，不会被清** → #338 无需为 segmentId 补透传。

## 动态阶段
- [x] Phase 0 评估 + 基线
- [x] Phase 1 fix subagent 实现
- [x] Phase 2 本地验证（4/4 绿）
- [x] Phase 3 commit + PR #344
- [x] Phase 4-6 review（3 路并行，1 轮 clean，无 fix 轮）
- [x] Phase 7 独立复核（编排者直读 App.tsx/AppShell.tsx + 独立复验 4 命令）
- [x] Phase 8 merge（用户授权，不等 CI）
