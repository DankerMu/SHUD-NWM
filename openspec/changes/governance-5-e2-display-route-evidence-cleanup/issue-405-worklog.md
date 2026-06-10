# Issue #405 Worklog — [Governance-5 E2-02] Clean mocked-vs-live evidence wording without reopening #365

## Roles / oracle
- Orchestrator: Claude Code (local). Docs-only → 直接编辑 + openspec validate（禁止改 frontend/playwright config，须 defer node-27）。
- 依赖 #404✓ #400✓（均 merged）。

## Ground truth (audit)
- no-broad-mock guard **已实现且 wired**：`assertLiveDisplaySpecsDoNotMockApis`（`playwright.config.helpers.ts:152`）→ `assertNoBroadApiRouteMocks`，在 `playwright.live-display.config.ts` 加载时执行；`liveDisplaySpecPattern=/(^|[/\\])live-display\.spec\.ts$/`、`broadApiRouteMockPattern=/page.route('**/api/v1/**')/`。
- live display lane = `apps/frontend/e2e/live-display.spec.ts`，profile `live-display-readonly`，script `test:e2e:live-display`（需 `PLAYWRIGHT_LIVE_BASE_URL`+`PLAYWRIGHT_LIVE_API_BASE_URL`，缺→`BLOCKED` 非 `PASS`）。
- mocked 回归 = 默认 `playwright.config.ts` / `--project=mocked-regression-chromium`。
- `docs/VALIDATION.md:1195+` 已完备表述 mocked 不可作 live receipt + live profile 要求；LEGACY_DEAD_CODE_INVENTORY 已有 #365/#366 分类。

## Changes
- `docs/governance/LEGACY_DEAD_CODE_INVENTORY.md` "Follow-Up Ownership" 追加 #405 注记：消费 #365 不重开；确认 guard 已实现+active；live lane=`live-display.spec.ts`；mocked specs 仍是 deterministic mocked regression、非 live receipt；无需 node-22 改码，未来 live spec 内 broad mock 泄漏属 node-27 follow-up。
- 不改 frontend/playwright config（acceptance 要求）；不动 #365 分类表行（避免"重开"），以 Follow-Up 注记给当前状态。

## Phase state
- [x] Phase 0 评估（#404/#400 merged，前置满足）
- [x] Phase 1 实现（inventory Follow-Up 注记）
- [ ] verify：openspec validate + 确认无 frontend 源码改动
- [ ] review：轻量 docs review
- [ ] merge：CI green → 自动 merge

## Decisions
- guard 已存在 → 2.4 无 code change 需求；不 split node-27 子 issue（条件未触发）。
- 不重写 #365 分类表行，新增 Follow-Up 注记承载当前状态，满足"不重开 #365"。
