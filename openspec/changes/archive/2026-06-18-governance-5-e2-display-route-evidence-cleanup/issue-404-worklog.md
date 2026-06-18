# Issue #404 Worklog — [Governance-5 E2-01] Align current display route authority docs

## Roles
- Orchestrator: Claude Code (local). Docs-only issue → 直接编辑 + openspec validate + route-authority grep（本 skill 的 docs-only carve-out，轻量处理）。
- Verify oracle: 本地（openspec validate + grep）。无 node-22/node-27 实机需求。

## Scope (from issue body + E2 tasks §1)
- `/` 是活跃单图展示入口；旧 display 路由 (`/overview` `/hydro-met` `/forecast` `/meteorology` `/flood-alerts` `/basins/:id` `/segments/:id`) 是 legacy redirect alias，不是活跃独立页。
- 当前 entrypoint docs: `README.md`、`progress.md`、`CLAUDE.md`、`docs/governance/DOC_STATUS.md`。
- Out of scope: 前端源码、移除 redirect、#342/#389、live receipt。

## Ground truth — apps/frontend/src/App.tsx 重定向矩阵
- `/` → OverviewPage（活跃单图）
- `/overview` `/hydro-met` `/forecast` → `<LegacyRedirect/>` → `/`
- `/meteorology` → `/?layer=met-stations`；`/flood-alerts` → `/?layer=flood-return-period`
- `/basins/:basinId` → `/?basinId=…`；`/segments/:segmentId` → `/?segmentId=…`
- `/monitoring` `/ops` `/system/model-assets` → 真实角色门控页（保留）

## Phase state
- [x] Phase 0 状态评估：无 PR / 无分支 / #400 closed（前置满足）
- [x] Phase 1 实现：README 路由表重写 + CLAUDE/progress 精确化 + DOC_STATUS 路由权威节
- [ ] Phase verify：openspec validate + route-authority grep
- [ ] Phase review：轻量评审（docs-only）
- [ ] Phase merge：CI green → 自动 merge（站点级预授权）

## Decisions
- 不新增 committed grep-guard 脚本：issue Tasks 原文为 "Add route-authority grep evidence in PR notes"，且对散文 docs 做 grep 强制易在历史 plans/runbooks 上误报。改为 (a) 修正 current-entrypoint docs，(b) DOC_STATUS 增"路由权威"节文档化，(c) 运行 grep 作为 PR 证据。
- 历史 plans/runbooks (`docs/plans/**`、dated runbooks) 中的旧路由按 DOC_STATUS 定义本就是 historical，不在 current-entrypoint 治理面内，保留。

## Candidate/Verdict ledger
(待评审)
