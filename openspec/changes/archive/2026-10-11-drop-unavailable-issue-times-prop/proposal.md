# drop-unavailable-issue-times-prop

## Why

`M11IssueTimeSelect` 的 `unavailableIssueTimes` prop 没有产品调用方（#2881 的 (C)）：只有两条单测在传。它是 `adapt-cycle-picker-retention-window` 的 delta spec 场景「known retained-out option is not selectable again」的预留接线点，而该场景在产品里从未成立——磁盘缺失过的时次仍在 `available_issue_times` 里，不带后缀、不禁用、可以再选。owner 裁定（2026-10-11，issue 评论）：放弃该场景，接受现状，删 prop。

## What Changes

- `apps/frontend/src/components/map/M11PopupChrome.tsx`：`M11IssueTimeSelect` 去掉 `unavailableIssueTimes` prop；不可用集合只剩“已选但不在列表里的保留时次”。
- `__tests__/M11RiverForecastPanel.test.tsx`：归一用例拿掉该 prop 的传参（其余断言不变）；“由 prop 指名一个在列表内、未选中的时次为不可用”的断言随 prop 删除，同一条用例里的缺省可访问名断言保留。
- `openspec/changes/adapt-cycle-picker-retention-window`（未归档）：delta spec 的该场景改写为产品现状；`tasks.md` 2.3 与 `design.md`（Approach 第 4 步、Rationale 末句、Test Plan 一条）同步（编排者改 openspec 文本）。

不改：两个产品调用方、`retainedIssueTime` 路径、文案、样式。design.md 省略（compact）。

## Triage

```text
Issue type: refactor (dead prop removal + spec correction)
Fixture level: compact
Upstream suggested level: compact (agree)
Blast radius: 共享组件 `M11IssueTimeSelect` 的 props；两个曲线窗的起报时次选择器
Selected risk packs: Legacy compatibility（两个活调用方与“已选时次被保留并禁用”路径不变）
Evidence floor: grep 零命中；保留路径的既有用例不改通过；typecheck / vitest / build；dist 比对；改了产品 JS 时 node-27 live 守卫
```

## Impact

- 受影响文件：`M11PopupChrome.tsx`、`__tests__/M11RiverForecastPanel.test.tsx`、`adapt-cycle-picker-retention-window/{specs/map-feature-popups/spec.md,tasks.md,design.md}`。
- 受影响规格：`map-feature-popups`。
