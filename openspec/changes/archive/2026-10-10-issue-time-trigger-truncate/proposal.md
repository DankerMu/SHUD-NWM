# issue-time-trigger-truncate

## Why

曲线窗里的起报时次触发器（`M11IssueTimeSelect`）高度固定（桌面 28px / 移动 44px）、限宽 192px，里面的值没有任何截断（#2870）。两种情形文字放不下：所选时次已被保留策略清掉时，带「· 磁盘保留不可用」的长标签折成两行、越出触发器上下边框（桌面与移动形态都有）；矮视口横屏的小屏（568×320，抽屉宽 284px）上河段窗起报条里触发器被说明文字「GFS + IFS 同步切换」挤窄，普通时次也折行出框，说明文字同时折成两行（320×568、667×375 只剩说明文字折行）。

## What Changes

- `apps/frontend/src/components/map/M11PopupChrome.tsx`（仅 `M11IssueTimeSelect`）：触发器里的值单行、放不下时省略号截断；触发器带 `title`，内容是所选时次的完整标签。
- `apps/frontend/src/components/map/M11RiverForecastPanel.tsx`（仅起报条 `m11-river-panel-cycle-bar`）：放不下时让位的是说明文字而不是触发器——触发器宽度不因说明文字变窄，说明文字要么单行完整可见、要么完全不可见。
- 新增一条移动 mocked spec 与「保留时次」夹具变体；桌面形态的一条用例并入既有桌面 spec `e2e/m11-sheet-controls-desktop.mocked.spec.ts`（见 tasks）。

不改 `src/components/ui/select.tsx`（#2861）、`M11PopupSourceControls`（#2848 删除）、下拉选项、长标签文案、桌面布局几何。design.md 省略（compact）。

## Triage

```text
Issue type: bugfix
Fixture level: compact
Upstream suggested level: compact (agree)
Blast radius: 河段窗与气象代站窗的起报时次触发器（桌面 + 移动形态）；改坏时触发器尺寸变化挤压图表区，或普通时次被省略
Selected risk packs: Legacy compatibility（桌面几何与既有移动 spec 不得移动）
Evidence floor: 新 mocked 用例先红后绿；既有抽屉 / 触控 / 图表下限 / 桌面碰撞 spec 不改期望值通过；node-27 上 PR 构建的 live receipt
```

## Impact

- 受影响文件：上述两个组件的类名与一个 `title` 属性、一个新 e2e spec、`e2e/support/` 下的夹具变体。
- 受影响规格：`map-feature-popups`。
