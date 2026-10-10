# stale-comments-and-doc-hygiene

## Why

六处文字 / 类型签名在讲代码里已经不存在的故事（#2871，H1–H6），外加 issue 评论里追加的两条同类漂移（H7、H8）：注释给出的不变量是错的、治理文档漏了现行 CI 车道、组件签名带一个没人读的参数、主 spec 的 Purpose 是归档占位句且让 `--strict` 校验非零退出。都不影响运行时，但会让下一个读者按错的前提改代码。

## What Changes

- H1 `m11MapRuntime.tsx`、H2 `useM11OverlayExpansion.ts`、H3 `e2e/m11-layer-basemap-launchers.mobile.mocked.spec.ts`、H8 `__tests__/M11StationForcingPopup.test.tsx`：只改注释 / JSDoc。
- H4 + H7 `docs/governance/LEGACY_DEAD_CODE_INVENTORY.md`：两格写明 mocked Playwright 车道在 `frontend-e2e`；`test:e2e:m15-visual` 一句与现状（独立配置、无 `--project`）一致。
- H5 `M11StationForcingPopup.tsx`：删除从未被读取的 `variable` prop（`StationVariableEcharts`、`StationChartArea` 及唯一传参处）。
- H6 `openspec/specs/pipeline-monitoring-frontend/spec.md`：`## Purpose` 写成真实用途（编排者直接改主 spec——delta 里的 Purpose 只在 capability 创建时读取）。

无运行时行为变化。design.md 省略（none）。

## Triage

```text
Issue type: refactor / docs
Fixture level: none
Upstream suggested level: none (agree)
Blast radius: 注释、一份治理文档、一个未读 prop、一份主 spec 的 Purpose
Selected risk packs: none
Evidence floor: src diff 除 H5 外全是注释行；typecheck / vitest；H6 的 strict 校验 exit 0；dist 内容不变
```

## Impact

- 受影响文件：上述六个文件 + `M11StationForcingPopup.test.tsx` 一行注释。
- 受影响规格：`doc-status-alignment`（delta）；`pipeline-monitoring-frontend`（仅 Purpose）。
