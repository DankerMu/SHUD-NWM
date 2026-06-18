# Issue #406 Worklog — [Governance-5 E2-03] Relabel historical M11/M15 visual evidence provenance

## Roles / oracle
- Orchestrator: Claude Code (local). Docs-only → 直接编辑 + openspec validate + gitignore 探针。
- 无 node-22/node-27 实机需求。

## Ground truth
- Tracked M11/M15 视觉资产 = 6 个 `apps/frontend/artifacts/m11-*.png`（basin+overview × 1280×900/1440×900/1920×1080），由 PR #160（`3e6fc48`，2026-05-18，M11 route-review）引入，mocked Playwright 视觉回归产物。
- M15 视觉 lane = spec `m15-visual-conformance.spec.ts` + 手动 `.github/workflows/m15-visual-evidence.yml`（`M15_EVIDENCE_SHA` pin）；#365/#366 已在 LEGACY_DEAD_CODE_INVENTORY 分类为 historical mocked，非 live。
- `.gitignore:53 apps/frontend/artifacts/` → 新生成物 ignored（探针 `probe-new-visual.png` 确认 ignored）。

## Changes
- `docs/governance/DOC_STATUS.md`：把 m11-*.png 段强化为 historical **mocked** visual evidence、显式"非 node-27 live proof"、补 provenance(#160/`3e6fc48`)+ 6 文件 index + 跨引 M15 manual lane / inventory + 重申新生成物 ignored、tracked 资产不移动。
- 不动 LEGACY_DEAD_CODE_INVENTORY（#405 要碰，串行避冲突）；DOC_STATUS 已跨引其分类。

## Phase state
- [x] Phase 0 评估（#400 closed，前置满足；#404 已 merge）
- [x] Phase 1 实现（DOC_STATUS relabel + provenance）
- [ ] verify：openspec validate + `git status`(无 stray artifacts) + 6 资产未动
- [ ] review：轻量 docs review
- [ ] merge：CI green → 自动 merge

## Decisions
- 不移动/重命名 tracked 资产（acceptance 默认）；provenance 以文档记录而非移动保全。
- index 落在 DOC_STATUS（artifact-ownership 权威），非新建 index 文件，避免 doc 增殖。
