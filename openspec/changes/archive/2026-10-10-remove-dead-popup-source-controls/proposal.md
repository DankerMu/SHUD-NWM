# remove-dead-popup-source-controls

## Why

`apps/frontend/src/components/map/M11PopupChrome.tsx` 导出的 `M11PopupSourceControls` 与 `M11PopupShell` 在产品代码里没有调用方（前者只被一个单测引用，后者全仓零引用），`useHydroMetPopupProduct.ts` 的 `M11_POPUP_SOURCES` 只为前者存在（#2848）。它们持续制造误导：设计文档把不存在的 UI 当成真实落点，约 70 行测试在给产品里不存在的控件做回归。owner 已决定删除。

## What Changes

- 删 `M11PopupShell`、`M11PopupSourceControls`（含 JSDoc）及因此变成未使用的 import；删 `M11_POPUP_SOURCES` 导出。
- `M11RiverForecastPanel.test.tsx`：`describe('M11PopupSourceControls')` 改写为直接渲染 `M11IssueTimeSelect` 的用例，保住“起报时次归一”这段行为的唯一覆盖；随组件死去的 GFS/IFS 按钮断言删除。两处因 testid 消失而恒真的反向断言换成基于 role 的断言。
- `openspec/changes/adapt-cycle-picker-retention-window/design.md` 里与现状不符的两步（向 `M11PopupSourceControls` 传不可用时次）按真实机制重写（编排者改）。已归档的 change 不动。

不改任何保留导出（`M11_POPUP_GLASS`、`formatIssueTime`、`M11PopupHeader`、`M11IssueTimeSelect`、`M11PopupLoading`、`M11PopupEmpty`）的签名与行为；三个产品调用方不动。design.md 省略（compact）。

## Triage

```text
Issue type: refactor (dead-code removal)
Fixture level: compact
Upstream suggested level: 未给出（按 compact：纯删除 + 测试改写，无运行时行为变化）
Blast radius: 曲线窗共用的 M11PopupChrome 模块；删错时河段窗 / 气象代站窗的头部或起报时次选择器编译失败或行为变化
Selected risk packs: Legacy compatibility（保留导出与三个调用方不动；归一行为的测试覆盖不丢）
Evidence floor: grep 零命中；改写后的归一用例对 M11IssueTimeSelect 有判别力（变异）；生产构建产物除删除的死代码外不变；完整 vitest 与 mocked 车道
```

## Impact

- 受影响文件：`apps/frontend/src/components/map/M11PopupChrome.tsx`、`useHydroMetPopupProduct.ts`、`__tests__/M11RiverForecastPanel.test.tsx`、`__tests__/M11StationForcingPopup.test.tsx`、`openspec/changes/adapt-cycle-picker-retention-window/design.md`。
- 受影响规格：`map-feature-popups`。
