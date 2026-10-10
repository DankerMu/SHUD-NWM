# remove-orphan-popup-product-hook

## Why

#2848 删掉 `M11_POPUP_SOURCES` 之后，`apps/frontend/src/components/map/useHydroMetPopupProduct.ts` 没有任何 import 方（#2881 的 (A)+(B) 部分）：里面的 hook 会去请求 `latest-product`，却没有人调用它。两份 API 契约治理文档和一份未归档的 design.md 仍把它写成该端点的现行消费方 / 现行机制，清单里还躺着一个已更名的文件 `M11RiverForecastPopup.tsx`。

## What Changes

- 删除 `apps/frontend/src/components/map/useHydroMetPopupProduct.ts`。
- `docs/governance/API_CONTRACT_FRONTEND_CONSUMER_EVIDENCE.md` 与 `docs/governance/API_CONTRACT_RETIREMENT_INVENTORY.md` 的 `latest-product` 消费方清单：去掉该文件，`M11RiverForecastPopup.tsx` 换成 `M11RiverForecastPanel.tsx`；端点为 `active`、无需迁移的结论文字不动，`Generated:` 日期不动。
- `openspec/changes/adapt-cycle-picker-retention-window/design.md` 的「Current Behavior」改成现行机制（两个曲线窗各自经 `fetchHydroMetLatestProduct` 取 `available_issue_times`）。

不在本 change：`M11IssueTimeSelect` 的 `unavailableIssueTimes` prop 与 delta spec 场景「known retained-out option is not selectable again」的去留（#2881 的 (C)，待 owner 裁定，issue 保持打开）。

## Triage

```text
Issue type: cleanup (dead module + doc correction)
Fixture level: none
Upstream suggested level: none (agree)
Blast radius: 无运行时行为；被删模块不在任何入口的依赖图里
Selected risk packs: 无
Evidence floor: 删除前零调用方复核；typecheck / vitest / build；改前改后 dist 按内容比对；文档清单里每个路径存在且确实调用该端点的取数函数
```

## Impact

- 受影响文件：上述 1 个源文件（删除）、2 份治理文档、1 份 design.md。测试文件零改动。
- 受影响规格：`map-feature-popups`（把现行取数路径写明）。
