# Tasks

- [x] 1.1 删除前复核零调用方：`git grep -n -E "useHydroMetPopupProduct|M11PopupProductModel" -- apps docs scripts tests packages .github` 除文件自身与两份治理文档外零命中；发现产品或测试调用方则停下报告。
- [x] 1.2 删除 `apps/frontend/src/components/map/useHydroMetPopupProduct.ts`。
- [x] 1.3 两份治理文档的 `latest-product` 消费方清单订正为 `stores/hydroMetProductData.ts`、`stores/stationLayerData.ts`、`components/map/M11RiverForecastPanel.tsx`、`components/map/M11StationForcingPopup.tsx`（只改清单这一句；其余文字、`Generated:` 日期、行号引用不动）。
- [x] 1.4 `adapt-cycle-picker-retention-window/design.md` 的「Current Behavior」第一句改成现行机制（编排者改 openspec 文本）。

## 约定

- Must preserve：测试文件零改动；`pages/m11/useHydroMetProduct.ts`、`pages/hydroMet/bootstrap.ts`、`M11IssueTimeSelect` 的签名（含 `unavailableIssueTimes`）不动；两份治理文档关于该端点 `active` / 无需迁移的结论零 diff。
- Non-goals：#2881 的 (C)；治理文档其他段落的重新盘点（含 `bootstrap.ts:57` / `:75` 这类过期行号——报告，不改）；已归档 change 的文本。
- Evidence floor：
  - `grep -rn "useHydroMetPopupProduct\|M11PopupProductModel" apps/frontend/src apps/frontend/e2e docs` 零命中；`grep -rn "M11RiverForecastPopup.tsx" docs/governance` 零命中。
  - 订正后清单的四个路径逐个存在，并贴出各自调用 `fetchHydroMetLatestProduct` / `loadHydroMetBootstrap` 的 `git grep` 行。
  - `cd apps/frontend && pnpm typecheck && pnpm test && pnpm build` 通过。
  - merge-base 与改后各 `pnpm build`，按内容比对 `dist`：预期 JS / CSS 逐字节相同（孤儿模块不在依赖图里；Tailwind 会扫描源文件，若 CSS 有差异逐条解释）。有任何 JS 差异则需 node-27 live 守卫，否则不需要。
  - `openspec validate remove-orphan-popup-product-hook --strict --no-interactive` 与 `openspec validate adapt-cycle-picker-retention-window --strict --no-interactive` 通过；markdown lint 通过。
