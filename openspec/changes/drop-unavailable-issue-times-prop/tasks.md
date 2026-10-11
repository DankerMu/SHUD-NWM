# Tasks

- [x] 1.1 `M11PopupChrome.tsx` 的 `M11IssueTimeSelect`：删 `unavailableIssueTimes`（解构缺省值、类型、`normalizedUnavailableIssueTimes`）；`unavailableSet` 收敛为只含 `retainedIssueTime`（可直接用 `time === retainedIssueTime` 判定，不必保留 Set）。其余逻辑、class、注释不动。
- [x] 1.2 `__tests__/M11RiverForecastPanel.test.tsx`：
  - 归一用例：删 `unavailableIssueTimes={[…]}` 传参；其余断言原样且必须仍通过（已选的过期时次本来就经 `retainedIssueTime` 进不可用集合，prop 在这条用例里对结果没有贡献）。
  - “disables an in-list unselected issue time named by unavailableIssueTimes and labels the trigger by default”：改写而不是删除——去掉 prop 传参，把它变成新规格的选择器级钉子：三个在列时次都不带后缀、都没有 `data-retention-unavailable`，点击原先被指名的 `cycles[1]` 会触发回调；缺省可访问名 `起报时间选择` 的断言全仓只在这条用例里，必须保留。用例名改成与内容相符。
  - 其他测试文件零改动。
- [x] 1.3 `adapt-cycle-picker-retention-window`：delta spec 场景「known retained-out option is not selectable again」改写为现状；`tasks.md` 2.3 与 `design.md`（Approach 第 4 步、Rationale 末句、Test Plan 一条）同步（编排者）。

## 约定

- Risk pack「Legacy compatibility」selected：`M11RiverForecastPanel.tsx`、`M11StationForcingPopup.tsx` 零改动；“已选时次不在列表 -> 置顶、禁用、带『磁盘保留不可用』后缀”的既有用例（两个测试文件）不改通过。
- Must preserve：`M11IssueTimeSelect` 其余 props 与缺省值；`labelOf` 的文案；触发器 `title`；归一逻辑（滤空白 / 去重 / 首尾空格）。
- Non-goals：给代站窗加“本会话失败时次”记忆；`M11StationForcingPopup.test.tsx` 里按行号引用 `M11PopupChrome.tsx` 的注释（行号会漂——报告，不改）。
- Evidence floor：
  - `grep -rn "unavailableIssueTimes" apps/frontend/src apps/frontend/e2e` 零命中。
  - 变异：把 `retainedIssueTime` 的禁用去掉，保留路径的用例应红（证明覆盖仍在）。
  - `cd apps/frontend && pnpm typecheck && pnpm test && pnpm build`。
  - merge-base 与改后 `dist` 按内容比对并逐条解释差异（预期只有含 `M11PopupChrome` 的块变小）；有 JS 差异则由编排者跑 node-27 live 守卫。
  - `openspec validate drop-unavailable-issue-times-prop --strict --no-interactive` 与 `openspec validate adapt-cycle-picker-retention-window --strict --no-interactive`。
