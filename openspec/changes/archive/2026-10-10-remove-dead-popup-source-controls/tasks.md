# Tasks

- [x] 1.1 `M11PopupChrome.tsx`：删 `M11PopupShell`、`M11PopupSourceControls`（连同其 JSDoc）；删因此未使用的 import（`M11_POPUP_SOURCES`、`HydroMetSource` 类型——tsc 不报未使用 import，逐个 grep 确认本文件内再无使用点后再删；`ReactNode`、`cn`、`normalizeIssueTimes`、`normalizeIssueTimeValue`、`M11_POPUP_GLASS` 仍在用，保留）。`useHydroMetPopupProduct.ts`：删 `M11_POPUP_SOURCES` 导出（`HydroMetSource` import 仍被使用则保留）。`useHydroMetPopupProduct.ts` 删掉该导出后整个文件成为孤儿模块（hook 与 `M11PopupProductModel` 全仓零调用方）——issue 明确把文件与 hook 留在范围外，本 change 不删，记为范围外观察另行立单；`M11IssueTimeSelect` 的 `unavailableIssueTimes` prop 同理（无产品调用方，签名保留）。删之前全仓 grep 复核零调用方（`apps docs openspec tests scripts packages .github`，排除 `node_modules` / `dist` / `openspec/changes/archive`）；发现产品调用方则停下报告。
- [x] 1.2 `__tests__/M11RiverForecastPanel.test.tsx`：
  - 把 `describe('M11PopupSourceControls')` 改为 `describe('M11IssueTimeSelect')`，直接渲染 `M11IssueTimeSelect`。保留并覆盖：空串 / 纯空白 / 首尾带空格 / 重复的 `issueTimes` 与 `unavailableIssueTimes` 归一后的选项集合；已选但不在列表内的时次被保留为首项、标「磁盘保留不可用」且禁用；**一个在列表内、未选中、由 `unavailableIssueTimes` 指名（传入时带首尾空格）的时次被禁用、带后缀与 `data-retention-unavailable`**——这是全套件唯一走“由 prop 指名不可用”路径的覆盖（其他用例走的都是“已选时次被保留”路径，后者无条件进不可用集合，对 prop 的归一没有判别力）；点选项后以归一值回调 `onIssueTimeChange`；触发器可访问名为 `起报时间选择`（该断言不得显式传 `ariaLabel`，测的是缺省值）；下拉的深色 class 断言若他处已有覆盖可不保留。
  - GFS / IFS 按钮的 `aria-pressed` 与点击回调断言随组件删除。
  - import 行改为取 `M11IssueTimeSelect`。
- [x] 1.3 两处恒真反向断言（`M11RiverForecastPanel.test.tsx` 与 `M11StationForcingPopup.test.tsx` 里的 `queryByTestId('m11-popup-source-controls')`）各换成 `expect(screen.queryByRole('button', { name: /GFS|IFS/ })).not.toBeInTheDocument()`（保留旁边注释记录的产品意图）。两窗里的 button 只有关闭键（“关闭面板” / “关闭弹窗”），图例与说明文字是 `span`、起报触发器是 `combobox`、气象代站的变量切换是 `tab`，所以这条断言安全；气象代站窗那条放在 `findByTestId('m11-station-popup-loaded')` 之后。
- [x] 1.4（编排者）`openspec/changes/adapt-cycle-picker-retention-window/design.md` 的第 3、4 步与现状不符：产品代码里没有任何地方向选择器传 `unavailableIssueTimes`，也没有 `M11PopupSourceControls`。按真实机制重写这两步——所选时次不在 `available_issue_times` 里时，`M11IssueTimeSelect` 把它前插为首项、标「磁盘保留不可用」并禁用（`M11PopupChrome.tsx` 的 `retainedIssueTime`）。已归档的 `mobile-responsive-display` 与 `2026-09-26-m11-popup-station-overlay-usability` 不动（issue 写于归档前，其第 5 条对 `mobile-responsive-display/design.md` 的修改因归档而不再适用）。

## 约定

- Risk pack「Legacy compatibility」selected：保留导出的签名 / 行为与三个产品调用方（`M11StationForcingPopup.tsx`、`M11RiverForecastPanel.tsx`、`M11DraggableCurveWindow.tsx`）不动；归一行为的覆盖不丢 -> 1.2 的变异。
- 未选：Public API、Config、File IO、Schema、Auth、Concurrency、Resource limits、Error handling、Release / dependency、Documentation。
- Must preserve：除 1.2 / 1.3 点名的用例外，两个测试文件的其他用例不改通过；e2e 不改。
- Non-goals：`M11IssueTimeSelect` 的任何行为或样式改动；开 `noUnusedLocals` / lint（#2863）；`docs/governance/LEGACY_DEAD_CODE_INVENTORY.md`（不登记导出级死代码）。
- Evidence floor：
  - `grep -rn "M11PopupSourceControls\|M11PopupShell\|M11_POPUP_SOURCES\|m11-popup-source-\|m11-popup-issue-time-empty" apps/frontend/src apps/frontend/e2e` 零命中；`M11PopupChrome.tsx` 的 import 段贴进报告。
  - 变异（sha256 还原）：`M11IssueTimeSelect` 里去掉 `issueTimes` 的归一（不滤空白 / 不去重）-> 改写后的归一用例红；去掉“保留已选时次”-> 对应断言红；去掉 `unavailableIssueTimes` 的归一 -> “由 prop 指名不可用”那条断言红；改掉触发器 `aria-label` 缺省值 -> 可访问名断言红。
  - 1.3 的新断言有判别力：临时在河段面板与气象代站窗里各渲染一个名为 GFS 的 button -> 各自红（还原）。
  - `cd apps/frontend && pnpm typecheck && pnpm test && pnpm build`；完整 mocked 车道；`openspec validate remove-dead-popup-source-controls --strict --no-interactive`。
  - 产物：merge-base 与改后各 `pnpm build`，按内容（不是按文件名）比对 `dist`。预期：JS 各块内容逐字节不变（死组件已被摇树去掉——以实测为准，若有 JS 差异逐条解释）；CSS 会变（`w-[min(30rem,90vw)]`、`ring-cyan-400/40`、`py-2.5` 等只在被删代码里出现的类不再被 Tailwind 扫描到；Tailwind 也扫描测试文件，改写的测试同样可能带来差异），CSS 文件名哈希与引用它的 `index.html` 随之变化。贴出 CSS 里消失 / 新增的规则，证明差异只来自被删代码与改写的测试。
  - node-27（编排者执行，PR 构建对 live API，verify checkout + 预览端口）：桌面 oracle 与 `mobile-portrait` / `mobile-landscape` 预设 exit 0（两类曲线窗照常打开、起报触发器在）。
