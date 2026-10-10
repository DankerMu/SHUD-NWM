# Tasks

- [x] 1.1 `M11IssueTimeSelect` 触发器：值所在的子元素（Radix `SelectValue` 渲染的 span，是触发器的直接子元素）单行 + 省略号截断（`text-overflow: ellipsis`）、可收缩（`min-w-0`）；箭头图标不被挤掉。截断类必须从触发器的 `className` 用子选择器下发（如 `[&>span]:…`；箭头是 `svg`，`>span` 只命中值）：Radix 2.2.6 的 `SelectValue` 会丢弃传给它的 `className` / `style`（`node_modules/@radix-ui/react-select/dist/index.mjs:220`），写在 `<SelectValue>` 上能过 tsc 但不生效；也不要给 `SelectValue` 传 children（会停止显示选中项文本）。触发器加 `title`，值为所选时次的完整标签（与选项里显示的同一字符串，含「· 磁盘保留不可用」后缀时也含）——把选项标签的拼接提成组件内一个局部函数复用，不新增导出。高度类（`h-7` / `mobile:h-11`）、字号类、`max-w-[12rem]` 的桌面取值不动；移动形态是否放宽上限由实现者按 1.3 的断言决定，放宽不是必须项。
- [x] 1.2 河段窗起报条：触发器的宽度不因说明文字而变窄（结果约束，手段不限；需要给触发器加类时经现有的 `triggerClassName` 传入，不改气象代站窗的调用）；说明文字「GFS + IFS 同步切换」不换行，放不下时整体不可见（不允许半截 / 省略号形态——10px 的说明文字截成半截没有信息量）。做法由实现者选最简单的一种（例如条为固定单行高、溢出裁剪的换行容器，或容器查询），条高必须等于现状（触发器高 + 计算上下内边距 + 计算下边线宽 1px，口径同 `e2e/m11-sheet-controls-desktop.mocked.spec.ts:39-40`），不随内容变化。
- [x] 1.3 新 mocked spec `e2e/m11-issue-time-trigger.mobile.mocked.spec.ts`（三个移动 project 都跑，用例内 `setViewportSize`，判据只用包围盒 / `scrollWidth` / `clientWidth` / 计算样式，不截图）：
  - (a) 390×664，河段窗与气象代站窗各一条，「所选时次不在 `available_issue_times`」状态：触发器高 44px、计算字号 ≥ 16px；触发器 `scrollHeight <= clientHeight` 且 `scrollWidth <= clientWidth`；值元素包围盒高度为单行（≤ 触发器 `clientHeight`）；值元素计算样式 `text-overflow` 为 `ellipsis`；值元素 `clientWidth` ≥ 「日期时间部分（`MM-DD HH:MM UTC`）+ `…`」的文本宽度（用同字体的离屏量具或 `Range` 量出，不钉死像素）；触发器 `title` 等于完整长标签；河段窗那条顺带断言说明文字可见且单行（390 宽放得下）。量取前的同步点：选中保留时次后窗进入空态、没有图表 canvas，等触发器文本出现「磁盘保留不可用」后缀（或 `m11-river-panel-empty` / `m11-station-popup-empty` 出现），不能复用“曲线已加载”判据。
  - (b) 568×320 与 320×568，河段窗、普通时次：起报条每个**可见**子项的包围盒高度 ≤ 触发器高度；说明文字要么不可见（`display:none`、被裁到条外、或零面积，判据写成“与条的包围盒不相交或不渲染”），要么单行且在抽屉水平范围内、与触发器不相交；触发器在抽屉水平范围内；触发器 `scrollHeight <= clientHeight` 且值元素包围盒为单行高（这两条才是“普通时次折行出框”的直接断言——值 span 是孙节点，不在“子项”里）；值元素 `scrollWidth <= clientWidth`（普通时次没有被省略）；起报条高度 = 触发器高 + 计算上下内边距 + 计算下边线宽（不钉死数字；改前即绿，属守卫）。
  - (c) 750×342 河段窗、普通时次：说明文字可见且单行（防止把说明文字在所有移动宽度都藏掉）。
  - 进入「保留时次」状态的夹具变体：带 `cycle_time` 的 latest-product 请求返回不含该时次的 `available_issue_times`（做法同 `src/components/map/__tests__/M11RiverForecastPanel.test.tsx` 的桩），作为**新函数**放在 `e2e/support/` 现有的 `curveSheet.mocked.ts` / `sheetControls.mocked.ts` 里，不得改 `installMultipleIssueTimes` / `installIssueTimes` 的回显语义（`m11-curve-sheet.mobile.mocked.spec.ts` 与 `m11-sheet-controls.mobile.mocked.spec.ts` 依赖“选中后触发器显示该时次且可用”）。机制：不带 `cycle_time` 的请求返回两个时次，带 `cycle_time=<较早时次>` 的请求返回只含最新时次的列表且 `cycle_time` 为最新时次；用例在下拉里选中较早时次，按真实交互走，不注入组件状态。
- [x] 1.4 桌面形态一条（必须在不带 `.mobile.` 的文件里——桌面 project 忽略 `*.mobile.*`；并入 `e2e/m11-sheet-controls-desktop.mocked.spec.ts` 新增一条用例，既有用例与期望值不动）：1280×800 河段窗「保留时次」状态，触发器高 28px、`scrollHeight <= clientHeight`、`scrollWidth <= clientWidth`、`title` 为完整长标签。
- [x] 1.5 vitest：`M11RiverForecastPanel.test.tsx` / `M11StationForcingPopup.test.tsx` 现有「保留时次」用例各补一条 `title` 断言（触发器 `title` 等于完整长标签）；普通时次时 `title` 等于普通标签。

## 约定

- Risk pack「Legacy compatibility」selected：桌面几何与既有移动 spec 是不可移动的参照 -> Must preserve 列表逐项不改期望值通过。
- 未选：Public API、Config、File IO、Schema、Auth、Concurrency、Resource limits、Error handling、Release / dependency、Documentation。
- Must preserve：`e2e/m11-overlay-collision.mocked.spec.ts`、`e2e/m11-sheet-controls-desktop.mocked.spec.ts`、`e2e/m11-sheet-controls.mobile.mocked.spec.ts`、`e2e/m11-touch-audit.mobile.mocked.spec.ts`、`e2e/m11-river-sheet-chart.mobile.mocked.spec.ts`、`e2e/m11-curve-sheet.mobile.mocked.spec.ts`、`e2e/m11-river-chart-touch.mobile.mocked.spec.ts` 不改期望值通过；触发器的 `data-testid`、`aria-label`、可访问名、选项列表与禁用语义不变；气象代站窗工具条的换行行为不变。
- Non-goals：`ui/select.tsx` 与字体优先级（#2861）；`M11PopupSourceControls`（#2848）；下拉选项的换行；长标签文案；触发器高度随内容增长的做法（会挤压图表区下限，已否决）。
- Evidence floor：
  - 1.3 / 1.4 先红后绿：在未改的 `src/` 上跑新 spec，如实报告每条红 / 绿（(a)、1.4 预期红；(b) 的两个视口预期至少因说明文字两行而红；(c) 预期绿——它是守卫）。
  - 变异（按 sha256 还原）：去掉值的截断类 -> (a) 与 1.4 红；去掉 `title` -> (a)、1.4、1.5 红；去掉说明文字的让位处理 -> (b) 红；把说明文字在移动形态一律隐藏 -> (c) 红。
  - `cd apps/frontend && pnpm typecheck && pnpm test && pnpm build`；完整 mocked 车道（`pnpm run test:e2e:mocked-regression`）；`src/__tests__/mobileSpecOracles.test.ts` 仍绿。
  - `openspec validate issue-time-trigger-truncate --strict --no-interactive`。
  - node-27（编排者执行，PR 构建对 live API，verify checkout + 预览端口）：桌面 oracle exit 0；568×320 与 390×664 下河段窗起报条单行、触发器文字不出框；live 上若无法进入「保留时次」状态，如实记为该状态只由 mocked 车道覆盖。
