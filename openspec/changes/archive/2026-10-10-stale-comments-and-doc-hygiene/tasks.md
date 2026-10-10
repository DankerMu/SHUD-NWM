# Tasks

行号以实现时的 master 为准（issue 的行号基于 `e0f5f7d6b`，其后 #2869 / #2848 改过相邻代码）。注释只陈述现在的事实与理由，不写“以前是什么”。

- [x] H1 `apps/frontend/src/components/map/m11MapRuntime.tsx`：帧回调里“抽屉……没有渲染出来就不平移——曲线面板渲染即崩溃而键仍非空时，挡住平移的只有这道闸”的理由不成立（曲线区域兜底时触发键就是 null；#2869 之后键变化还会取消待执行的帧）。改写为真实理由：必须量真实渲染出来的抽屉，因为形态切换时页面先翻、抽屉还是旧形态（指向该 hook 头注释的同一段）。动手前重读 #2869 合入后的 effect，确保新注释与现在的代码逐句相符。diff 只含注释行。
- [x] H2 `apps/frontend/src/pages/m11/useM11OverlayExpansion.ts`：`collapse` 的 JSDoc 与 hook 头 JSDoc 如实列出——`collapse` 的调用方（以 `git grep -n collapse -- apps/frontend/src/pages/OverviewPage.tsx` 为准：地图点击、让位复位）；`Escape` 与形态切换不经 `collapse`、直接复位；去掉“（4.x）”前向引用。diff 只含注释行。
- [x] H3 `apps/frontend/e2e/m11-layer-basemap-launchers.mobile.mocked.spec.ts`：“运维入口比启动器宽，会把面板的锚点往左推”的注释改为与现状一致（运维入口与启动器同宽 44×44；断言“面板整个留在地图区内”仍然有效，写它现在守的是什么）。断言不动，diff 只含注释行。
- [x] H4 `docs/governance/LEGACY_DEAD_CODE_INVENTORY.md`：`.github/workflows/ci.yml` 行与 `apps/frontend` 行写明 mocked Playwright 车道在独立的 `frontend-e2e` matrix job（显示名 `Frontend E2E (mocked) (<片号>)`），`frontend-build` 只做 typecheck / build / vitest / bundle；与 `.github/workflows/ci.yml` 现状核对后落笔。文件头 `Updated:` 行更新为 2026-10-10。
- [x] H7 同一文件里把 `test:e2e:m15-visual` 与 `mocked-regression-chromium` project 写在一起的那句改为与现状一致（该 script 用独立配置 `playwright.m15-visual.config.ts`，不带 `--project`；默认 mocked 车道的任何 project 都不执行该 spec）。
- [x] H5 `apps/frontend/src/components/map/M11StationForcingPopup.tsx`：删除 `StationVariableEcharts` 的 `variable` prop（解构与类型），连带删除 `StationChartArea` 的 `variable` prop 与其唯一调用点的传参。删前确认函数体内确无读取；若 `HydroMetStationSeriesVariable` 的 import 因此未使用则一并删（tsc 不报未使用 import，grep 确认）。
- [x] H8 `apps/frontend/src/components/map/__tests__/M11StationForcingPopup.test.tsx`：引用 `M11PopupChrome.tsx:97-127` 行号的注释改为引用符号名（`M11IssueTimeSelect` 的 `retainedIssueTime`）。
- [x] H6（编排者）`openspec/specs/pipeline-monitoring-frontend/spec.md` 的 `## Purpose` 写成真实用途；`## Requirements` 以下零 diff。

## 约定

- Must preserve：除 H5 的 prop 删除外，`apps/frontend/src/**` 与 `apps/frontend/e2e/**` 的 diff 全是注释行；不改任何断言、行为、其他 inventory 行。
- Non-goals：平移 / 展开值的行为；其余 201 份占位 Purpose 的主 spec；`noUnusedLocals` 等门禁（#2863）；孤儿 hook 文件（#2881）。
- Evidence floor：`git diff` 逐文件证明“只含注释行”（H5 除外）；`cd apps/frontend && pnpm typecheck && pnpm check:types && pnpm test && pnpm build`；改前 / 改后 `dist` 按内容比对并逐条解释差异（实测：只有 `OverviewPage` 块因 H5 少了两个未读属性而变化，故加跑 node-27 守卫）；`pnpm exec playwright test e2e/m11-layer-basemap-launchers.mobile.mocked.spec.ts`（三个移动 project）；`uv run pytest tests/test_entropy_audit_retired_paths.py -q`；`openspec validate pipeline-monitoring-frontend --type spec --strict --no-interactive` exit 0；`openspec validate stale-comments-and-doc-hygiene --strict --no-interactive`。node-27：H5 改变了 `OverviewPage` 块，编排者在 PR 构建上跑移动预设与桌面 oracle 作为守卫。
- [x] H9（编排者，来自 PR #2884 的 P2）：`openspec/changes/adapt-cycle-picker-retention-window/design.md` 第 3 步的“a retained-disk miss only produces the empty state of step 5”与代码不符——单源 miss 时另一源照常画图、原因写在图下的 partial 提示里，两源都不可绘制才进空态。订正这一句（对照 `M11StationForcingPopup.tsx` 的 partial / empty 分支）。
