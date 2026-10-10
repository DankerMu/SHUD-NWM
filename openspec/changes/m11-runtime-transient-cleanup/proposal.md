# m11-runtime-transient-cleanup

## Why

M11 地图 / 曲线窗运行时有三处“瞬时提交或卸载留下没收回的副作用”（#2869）：(1) `useM11SelectedAnchorCamera` 排出的帧不保存句柄，同一帧内触发键 A -> '' -> A 会发两次 `easeTo`，卸载后已排的帧也不取消；(2) `M11DraggableCurveWindow` 拖拽进行中被卸载时，window 上的 `pointermove` / `pointerup` / `pointercancel` 三个监听不清理；(3) 移动形态下曲线面板首次渲染即崩溃时，那一次瞬时提交把让位信号传给时间轴，播放被暂停，抽屉却从未出现。

## What Changes

- `apps/frontend/src/components/map/m11MapRuntime.tsx`（仅 `useM11SelectedAnchorCamera`）：保存帧句柄；键每次变化（含变空）与 effect 清理 / 卸载时取消尚未执行的帧。
- `apps/frontend/src/components/map/M11DraggableCurveWindow.tsx`：卸载时走 `endDrag`。
- `apps/frontend/src/pages/OverviewPage.tsx`：传给底部控制条的让位信号延后一个 effect 周期（隐藏与暂停一起），不再包含“面板首次渲染即崩溃”的那次瞬时让位；正常打开抽屉“即暂停、关闭不续播”不变。
- 三个既有 vitest 文件各补先红后绿的用例。不新增 / 不改 e2e。

不改 `RegionErrorBoundary`、`M11Timeline` 的 prop 语义、桌面形态任何行为；不处理“关窗时取消进行中的平移动画”（规格文字互相矛盾，待 owner 裁定）；不做兜底后自动续播。design.md 省略（compact）。

## Triage

```text
Issue type: bugfix
Fixture level: compact
Upstream suggested level: compact (agree；若实现期需要动 M11Timeline 的 prop 语义或 RegionErrorBoundary，停下报告，不自行上调)
Blast radius: 移动形态抽屉自动平移、曲线窗拖拽、打开抽屉时的自动暂停；改坏时平移不发生 / 多发、拖拽失灵、打开抽屉不暂停
Selected risk packs: Concurrency / shared state / ordering；Error handling
Evidence floor: 三组新 vitest 先红后绿 + 变异；点名的 must-preserve vitest 与 e2e 不改通过（auto-pan 与 yields-chrome e2e repeat-each=5 无 flaky）；node-27 上 PR 构建的移动预设与桌面 oracle
```

## Impact

- 受影响文件：上述三个源文件与三个测试文件（其中 `M11MapLibreSurfaceSheetAutoPan.test.tsx` 的 rAF 夹具要改成可取消）；不碰 `M11BottomControlBar.tsx` / `M11Controls.tsx`。
- 受影响规格：`mobile-curve-sheet`。
