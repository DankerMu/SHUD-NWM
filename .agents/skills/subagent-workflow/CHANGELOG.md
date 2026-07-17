# Changelog

All notable changes to this skill will be documented in this file.
This project adheres to [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.19.1] - 2026-07-16

### Changed

- 描述减重（常驻上下文税）：frontmatter description 从 655 字符压至 ~450——删去为弱触发模型准备的触发词枚举（保留 "implement #XX"/"处理下一个issue" 两个最强触发与全部反触发语义），语义边界不变。未跑 skill-creator 触发评测（无 ANTHROPIC_API_KEY），实际触发率变化需在使用中观察。

## [0.19.0] - 2026-07-16

### Added

- **轮间 lens 轮换（带测量的试运行）**：修复后复审轮的 reviewer 组合从"与 Phase 4 同配置重跑"改为**钉死核心 + 自由槽轮换**——fixture 风险包选中的 lens 每轮必在（承担修复回归召回），其余槽位轮换为本 PR 尚未用过的 reviewer 包（同 lens 轮次共享盲区，轮换以零边际成本买联合召回）。禁止轮换掉任何钉死的风险包 lens。落点：phase-flow Phase 4 review rounds / Phase 6.5、phase-4-cross-review follow-up 规则。
- **日志归因字段**：`review-loop-log.jsonl` 行新增 `round_lenses`（逐轮实际 lens 组合）与 `catches`（每条 net-catch finding 的轮次/产出 lens/失效类/严重度），把"后续轮的捕获来自钉死核心还是轮换进来的 lens"变成可查询事实。
- **轮换 keep/cut 裁决标准**（沿用既有 ADR 人工决策机制，样本 ≥8 个多轮 PR）：后续轮捕获集中于轮换 lens → 留任；几乎全部来自钉死核心对修复区新代码的回归召回 → 轮换无收益，记录 ADR 并回退为 round-1 同配置。试运行政策靠机制论证上线，靠净捕获归因数据留任。

## [0.18.0] - 2026-07-16

### Changed

- **Phase 4.5 verifier 从 per-finding 改为 per-failure-class 批量**：去重后的 candidate 先按 failure class 分组，每个 class 批次一个 `verifier` 子代理，批内至多 5 条（超出拆批；单条 class 退化为单条批，行为同旧版）。成本随 class 聚类因子下降；同 class 兄弟 finding 共享证据基，同一验证者一起裁决还能提高 verdict 一致性、捞出 dedup 漏掉的近重复。
- 逐条裁决语义不变且硬化：每条 candidate 一个独立 verdict（CONFIRMED/PLAUSIBLE/REFUTED）+ 逐条证据，"批级一口价"判定无效、整批重跑；批内发现同一缺陷的两条 candidate 须各自 verdict 并在 note 标注重复，不得静默合并。
- 独立性规则随批量调整：verifier 不得是产出**该批内任一** candidate 的 reviewer；编排器不得代裁不变。verdict 持久化从 `verify-<CANDIDATE_ID>.md` 改为按批 `verify-<CLASS_ID>.md`（逐条 verdict 表）。
- 同步三处：SKILL.md 核心规则、`phase-flow.md` Phase 4.5 步骤 3/4、`phase-4-cross-review.md` verifier 模板（新增 `<CLASS_ID>`/`<CANDIDATE_BLOCKS>` 变量与批量输出表）。

## [0.17.0] - 2026-07-16

### Added

- **Review Failure Retro 新增第四个 failure shape `converging`**，修补 0.16.0 三轮 gate 在健康收敛轨迹上的误触发：运行数据表明多轮 review 每轮都有净捕获，"第 3 轮不 clean"是常态而非病态信号，但 0.16.0 的三个 shape（breadth/depth/noise）没有一个描述健康收敛，默认动作（拆 PR/重构/降级）对它全是错误处方。
- `converging` 判据（须在 retro 中列出逐轮数字作证据）：各轮已验证 finding 无同类复发，数量与最高严重度逐轮不增、至少一项严格下降；第 3 轮出现任一 critical/major 或任何同类复发即丧失资格。
- `converging` 默认动作：**有界延长**——ordinary loop 至多再跑 2 轮 comprehensive cross-review，retro 只写一段收敛趋势、跳过策略章节；每 PR 至多选择一次。第 5 轮仍不 clean 则重进 gate，`converging` 不再可选，必须从 breadth/depth/noise 三选一。实质效果：3→5 轮之间的梯子由收敛证据驱动重建，gate 对病态轨迹仍是硬转向，对健康收敛只收一段话的税。
- Post-gate budget 拆分为两支：pivot 支（breadth/depth/noise，纠正动作后至多一轮、仍有 critical/major 则升级重进）与 converging 支（硬 2 轮上限、round 5 强制重进且排除 converging）。

## [0.16.0] - 2026-07-16

### Changed
- **审查升级 gate 从 5 轮提前到 3 轮，四文档包降为单份 retro**：删除五轮硬 gate 全套（Deep Review Failure Retro + Gate-Level PR Strategy Review + Invariant Surface Inventory + Regression Matrix 四文档包及 post-five budget）——触发太晚、仪式过重。新的唯一硬 gate：第 3 轮 comprehensive cross-review 仍不 clean（不再限定同一 failure class）即停 ordinary loop，持久化一份升级版 Review Failure Retro 后按归因执行纠正动作。
- **Review Failure Retro 升级**：模板新增 PR/SHA/轮次证据行与 `Failure shape` 归因（breadth 分散 / depth 同 invariant 反复 / noise 评审噪音），并绑定默认动作映射——breadth 默认**拆 PR**（子 PR 以新 PR 身份重进工作流、round counter 归零，父 PR 证据束记录拆分方案与 finding 归属）；depth 默认 refactor/redesign 或诊断任务（**禁止拆分反复失败的 invariant**：每个子 PR 都继承同一缺陷）；noise 默认 reviewer-pattern downgrade 并记录理由。偏离默认动作须在 retro 中记录原因。
- Post-gate budget 随 gate 前移：纠正动作后至多一轮 comprehensive cross-review，仍有 critical/major 则带更新后的 retro 重进 gate 并选更强动作，不得回退到逐行修补。
- SKILL.md 不可协商项同步：由"第 6 轮前必须持久化 Gate-Level 包"改为"第 4 轮前必须持久化 Review Failure Retro 并执行其纠正动作"；round counter 不重置规则补充唯一合法例外——gate 选定的 PR split 产生的子 PR。

## [0.15.0] - 2026-07-14

### Added
- Fixture 契约新增 **Seams under test** 字段（compact 与 expanded 模板各一处，`references/issue-risk-contract.md`）：测试将行使的公共边界，由上游（`stage-change-pipeline` Stage 2 design.md 或 issue 作者）**预先声明并附理由**——最少、最高、理想一个，无需人工确认，由 fixture review 检查。Core Rule "OpenSpec is the fixture" 同步列入：实现期只消费、不再谈判，需要但缺失的 seam 是一条须上报的 deviation。Adapted from `mattpocock/skills` v1.1.0 `tdd` 与 `to-spec`（先画缝再写 spec）；上游的用户确认环节有意改为上游自动声明 + 审核监督。

## [0.14.0] - 2026-07-14

### Added
- **诊断闸门（cause-unknown 专用）五个绑定点**，消费新落地的 `diagnosing-bugs` 0.1.0（canonical 诊断词汇；consume-don't-fork，对齐 `risk-adaptive-cross-review` 先例）：
  - Core Rule "Self-repair by delegation" 加限定：失败原因无法从输出确定时，先派**诊断任务**（报告契约 = 红命令+输出、最小复现、确诊假设+证据，不含修复），其报告才使 fix task 成为 "precise"。
  - Phase 2：原因不明显的验证失败先走诊断 brief，禁止凭猜写修复任务。
  - Phase 5：诊断闸门规则——因未知的 finding/失败需先有"已跑过一次、能红"的命令才进 fix list；有精确 file/line 且原因显然的普通 finding 不付诊断税；上一轮修复未关闭的 finding 重新过闸。
  - Phase 6：新增**诊断 brief 模板**（implementer 子代理执行、report-only、orchestrator 内联蒸馏纪律——叶子子代理不触发技能）；因未知类修复的 `Test:` 字段硬化为"先红后绿"的红命令；插桩必须带 `[DEBUG-<tag>]`，提交前 `grep -r "DEBUG-"` 清零；seam 缺失本身就是 finding，走 deferral 路由。
  - 两个 Retro（Review Failure / Deep Review Failure）各加归因行 "Cause never diagnosed (no red repro before fixes)"。
  - Phase 8：本地不复现的 CI 失败先诊断（CI 环境 vs 本地的差分回路）再分类 `ci-only`/`semantic`，不按日志表面形状分类。
- Supporting Skills 新增 `diagnosing-bugs` 条目（Phase 0-8 之外的交互式调试直接用独立技能）。

## [0.13.0] - 2026-07-06
- New core rule "Deviations are recorded, not silent" (adapted from Thariq's implementation-notes practice): Phase 1 implementer briefs and Phase 6 fix briefs must report every departure from the plan (unexpected upstream/API behavior, non-reusable component, switched implementation path, mid-work constraint) as one line each — what/why/impact — with "no deviations" stated explicitly.
- The PR description carries a running `偏离记录` section (seeded at Phase 3, appended by Phase 6 fix passes); Phase 4 reviewer briefs include it so review attention goes first to where the implementer chose in territory the plan did not cover; the Phase 8 Chinese work summary consumes it as a new `计划偏离` section.
- Complements 0.12.0 deferral routing: deferrals track what was found but not done; deviations track what was done differently than planned. Reuses the PR body — no new artifact file.

## [0.12.0] - 2026-07-06

- **Deferrals are routed, not dropped** (new Core Rule + Phase 8 gate bullet): every explicitly deferred verified finding and every 剩余风险/已知限制 entry in the merge-time work summary must carry either a tracked issue URL — delegated to the `issue-scribe` agent when installed (verify → dedup → one structured issue → URL) — or a recorded one-line reason why no issue is filed. An unrouted deferral fails the pre-merge gate instead of silently dropping. This makes issue-scribe's trigger structural (gate-checked at the places deferrals are born: the cross-review deferral rule, the Chinese work-summary template's 已知限制 line, the gate itself) rather than description-driven.
- `issue-scribe` added to the optional-prerequisites line alongside `worktree-guard` and `monitor`.

## [0.11.0] - 2026-07-06

- **Verification matrix becomes a standing project asset** (eng-init alignment): the project profile grows from six to eight fields — Phase 0.0 bootstrap now also records the repo's real command entry points (package scripts / Makefile / justfile / CI steps) and a verification matrix (`surface -> command -> evidence`). Phase 2 executes the matrix rows for the touched surfaces plus the default build+test row instead of improvising a pipeline each run (falling back to discovery only when a row is missing, then writing it back via Phase 0.5 profile-gap maintenance), and the Phase 8 completion self-audit treats a touched-surface matrix row never executed on the final head as an uncovered criterion. Templates in `project-profiles.md` (contributes-list, Generic, authoring block, size budget).
- **Route control-plane bootstrap upstream**: new Supporting Skills entry pointing to `control-plane-auditor` (codebase-stewardship) for repos that lack instruction files, unified command entry points, or verification infrastructure; its bootstrap mode scaffolds what Phase 0.0 consumes.

## [0.10.0] - 2026-07-06

- **Wire in the `worktree-guard` hook (optional)**: worktree-delegation mode is now bracketed by `.worktree-guard.json` at the project root — written on entry with `allowedRoots` covering the delegated worktrees plus orchestrator-owned `.workplans/` and `openspec/`, deleted after integration and `git worktree prune` — so out-of-root file writes are denied mechanically instead of by prompt discipline. Added the entry/exit/session-restart protocol and scope notes (file-edit tools only; `git apply` in the shell unaffected) to `references/parallel-worktree-delegation.md`, the bracket rule to the `SKILL.md` parallel-isolation Core Rule, and a stale-guard-file warning. Without the hook installed everything stays orchestrator-enforced.
- **Wire in the `monitor` agent (optional)**: the Silent-long-waits Core Rule and `phase-flow.md` Phase 8 now prefer delegating harness-external waits (CI runs) to a single `monitor` subagent — ID-based completion detection, one quiet blocking wait, read-only — over orchestrator-side polling loops.
- **Declare both as optional prerequisites** in `SKILL.md` so the wiring is discoverable at install time.

## [0.9.0] - 2026-07-02
- **Realign the standard reviewer set to canonical `risk-adaptive-cross-review` (`reviewer-packages.md`)**: the four standard reviewers are Correctness, Integration, Security/Performance, and Test & Evidence Coverage; the six-reviewer high-risk escalation adds Spec Compliance and Invariant/State-Machine/Compatibility. Fixes the prior inversion in `phase-4-cross-review.md` (roles table + high/broad-expanded selection) and `phase-flow.md` Phase 4 that made Spec Compliance a base reviewer and demoted Test & Evidence Coverage to escalation-only.
- **Carve out legitimately-unreviewed `none`-tier PRs from the pre-merge hard-gate**: the gate now passes with EITHER (a) SHA-matched review artifacts (Agent Review block, Phase 4.5 verdict table, clean latest comprehensive round, Phase 7) OR (b) a persisted "review not required" record (fixture tier `none` + the Phase 2 audit that found no risk, recorded against the final head SHA). Missing both remains a skip block. Removes the contradiction with Phase 4 `none`-tier skip. (`SKILL.md` gate rule + `phase-flow.md` Phase 8 gate bullets.)
- **Fix the report-persistence mechanism for read-only subagents**: reviewer and verifier subagents have no write access, so they now RETURN the complete report/verdict as their final message and the orchestrator persists it to `<REVIEW_DIR>`. Rewrote the `phase-4-cross-review.md` reviewer and verifier brief instructions, added an orchestrator-side collection sentence in `phase-flow.md` Phase 4, and clarified the same return-then-persist contract for the read-only fixture reviewer in `issue-risk-contract.md`.
- **Define one canonical evidence root**: `<REVIEW_DIR>` defaults to `.workplans/<issue-or-pr>/review/` (aligned with `parallel-worktree-delegation.md`'s `.workplans/<issue-or-pr>/`) and is referenced everywhere evidence is persisted (`phase-4-cross-review.md` variable def; `phase-flow.md` Phase 4/4.5/8).
- **Clarify the leaf-task explorer boundary**: worker agent definitions may natively permit spawning an `explorer` subagent for standalone use; inside this workflow's leaf tasks that capability is disabled and the injected boundary overrides the agent definition. Added to the `SKILL.md` Required Subagent Boundary and the `phase-4-cross-review.md` reviewer brief boundary.
- **Record the rollback anchor explicitly**: after each clean comprehensive cross-review round the orchestrator records `Last clean reviewed SHA: <sha>` in the evidence bundle; the Phase 8 corruption-recovery path resets to that recorded SHA. Corrected the `phase-flow.md` Phase 8 paragraph that wrongly called the frozen final HEAD the clean rollback anchor.
- **Always persist the project profile**: Phase 0.0 bootstrap always writes `openspec/project-profile.md`, stamping a pure-Generic result as Generic with a note that nothing project-specific was found. Fixed the Phase 0 lookup-order step that implied an in-memory-only Generic fallback.
- **Fix the phase skeleton both directions**: added Phase 0.0 (profile bootstrap) to the `SKILL.md` skeleton, and split the post-fix rerun out of Phase 6's tail into its own `## Phase 6.5: Repeat Cross-Review After Fixes` heading in `phase-flow.md` (which the Chinese work-summary template already referenced).
- **Complete the verifier-bias sentence in `SKILL.md`**: added the third `expanded` tier (PLAUSIBLE blocks only when it maps to a selected risk pack or Invariant Matrix row) alongside the existing `high`/`broad-expanded` and `low`/`compact` tiers.
- **Stop overclaiming "deterministic"**: reworded the `SKILL.md` gate rule and `phase-flow.md` Phase 8 paragraph so only the SHA-match and artifact-presence clauses are deterministic; the completion self-audit and oracle-integrity clauses are mandatory checkable procedures that require reviewer judgment. A failure of any clause still blocks the merge.
- **Split the garbled Phase 4.5 sentence** into two correct rules: drop every REFUTED candidate (with recorded rationale); a `wontfix` test-coverage exception remains forbidden.

## [0.8.1] - 2026-07-02
- Trim the SKILL.md description from ~1000 to ~640 characters: compress the feature enumeration into one clause while keeping every trigger phrase and the full Do-NOT / cc-cx disambiguation. The old run-on description was diluting the trigger signal and brushing the practical description-length ceiling.

## [0.8.0] - 2026-06-18
- Sharpen Phase 7 into the canonical **Gap Sweep** (defined in `risk-adaptive-cross-review` Synthesis): the independent final reviewer now runs a fresh clean-slate pass with the verified-findings list visible, looking only for real defects the recall-biased rounds systematically miss (removed behavior, contract drift, boundary/error/async/auth/migration/cache/wrapper paths), in addition to test coverage vs `tasks.md` and consumer compatibility. Consumes the canonical definition instead of restating it.
- Add a **completion self-audit (premature-completion guard)** and an **oracle-integrity** check to the Phase 8 pre-merge evidence hard-gate, and reflect both in the always-loaded `SKILL.md` gate rule: before merge the orchestrator re-derives each acceptance criterion and selected task and confirms the diff/tests actually satisfy it (not "the agent said done"), confirms no required edge/error path is unhandled and the changes are internally consistent, and confirms no test/spec/CI was weakened to pass. The frozen final HEAD is the clean rollback anchor — a gate failure blocks the merge (deterministic, no "probably fine"), and a fix round that corrupts a clean reviewed state resets to the last clean reviewed SHA rather than patching a broken head. Adapted from stellarlinkco/skills `harness` (validation-required completion, started-commit rollback) and `code-review` (oracle non-tampering), clean-room with author permission.

## [0.7.0] - 2026-06-18
- De-duplicate the cross-review reviewer definitions (entropy reduction): collapse the six self-contained reviewer briefs in `references/phase-4-cross-review.md` into one assembly template plus a reviewer-role table, and inline each reviewer's checklist and cross-cutting lenses from the canonical `risk-adaptive-cross-review` (`reviewer-packages.md`) instead of restating them. This makes phase-4 match what `SKILL.md` already claimed — that the reference holds only scaffolding, the Invariant Matrix binding, and the Phase 4.5 verifier — and removes the two-copy drift that any future reviewer-checklist or lens change would otherwise have to maintain in both places.

## [0.6.0] - 2026-06-18
- Add a hard pre-merge evidence gate (meta-loop dimension 8): never merge (including pre-authorized auto-merge) unless the PR `Agent Review` block, the Phase 4.5 verifier verdict table, a clean latest comprehensive cross-review round, and Phase 7 all exist and are SHA-matched to the frozen final HEAD. Recommend enforcing it as a host-repo required CI/branch-protection status check so skipping the review/verify loop is a detectable hard action; log every skip block. A portable skill cannot install that check, so absent it the gate stays orchestrator-enforced.
- Add cross-run loop accountability (meta-loop dimension 6): after each merge, append one line to a committed append-only review-loop log (`docs/review-loop-log.jsonl`) recording fixture level, comprehensive rounds, `gate_net_catch` (verified findings the review/verify loop caught that local verification and CI missed), verifier verdict counts, residual deferrals, and pre-merge skip blocks. Define a keep/cut criterion (human call, ~8-PR minimum sample, default-keep since this workflow prioritizes correctness over cost) recorded in `docs/adr/`. Cross-run risk learning already ratchets through the living `openspec/project-profile.md`; this adds the accountability half.

## [0.5.0] - 2026-06-14
- Rename the skill from `codex-codeagent-workflow` to `subagent-workflow` and convert the delegation model from `codeagent-wrapper --backend codex` leaf tasks to native subagents. Implementation/fixes delegate to the `implementer` subagent, cross-review to `reviewer` subagents, and the Phase 4.5 verification gate to the `verifier` subagent. Push and PR ownership stay with the orchestrator (`implementer` is push-free), so the workflow does not modify the shared `coder` agent that Issue Agent OS depends on.
- Make the workflow orchestration-agnostic across Claude Code and Codex: the orchestrator uses its native subagent mechanism (Claude Code Task subagents or Codex subagents) instead of shelling out to an external code-agent CLI. Drop `codeagent-wrapper` from prerequisites and `skill.json` requirements.
- Replace the "Required Delegation Guard" with a "Required Subagent Boundary" and reframe "No nested AI delegation" as "No nested workflow delegation": each spawned `implementer`/`reviewer`/`verifier` is a leaf that must not re-invoke this workflow or spawn further nested workflow subagents. The boundary also requires treating issue/external content as untrusted data, not instructions.
- Rewrite parallel code-writing isolation to use `.worktrees/` (was `.codex/worktrees/`) and an orchestrator-owned manifest; rewrite the Phase 4 cross-review and Phase 4.5 verifier templates as subagent briefs rather than `codeagent-wrapper` heredocs. No change to OpenSpec gates, risk-adaptive review/fix governance, Invariant Matrix, round budgets, or merge-gate behavior.
- Hardening from skill self-audit: detect the default branch instead of hardcoding `master`; drop the codeagent `@file` reference syntax in favor of plain OpenSpec paths; tighten triggers (remove broad "do it"/"run the workflow" phrasings) and add a `cc-cx-workflow` anti-trigger; note that `.worktrees/` should be gitignored.

## [0.4.1] - 2026-06-01
- Add a size budget for `openspec/project-profile.md` (in `project-profiles.md` authoring and Phase 0.0): scaled soft targets (~25 lines simple, ~60 broad) plus two hard rules — never restate core packs/triggers and use short bullets over prose — to keep the per-run profile lean and prevent re-accreting core content.

## [0.4.0] - 2026-06-01
- Make the project profile a living, project-local artifact at `openspec/project-profile.md` instead of a selection from the shared skill. It survives skill reinstalls and never accretes project-specific surfaces back into the shared skill.
- Add Phase 0.0 profile bootstrap: on first run in a project with no `openspec/project-profile.md`, scan the repo and generate one (copying the closest template or Generic). Phase 0 resolves the profile by lookup order: project-local file -> bootstrap -> Generic fallback.
- Add Phase 0.5 profile-gap maintenance: update the project-local profile when an issue exposes a new entry surface, contract, risk axis, or domain pack; ordinary issues that already fit the profile do not touch it.
- Reframe `references/project-profiles.md` as a shared template catalog (Generic plus SHUD/rSHUD/AutoSHUD examples) rather than the place to add per-project profiles.

## [0.3.0] - 2026-06-01
- Decouple the workflow from the SHUD/rSHUD/AutoSHUD project family: extract the three hardcoded profiles into a new pluggable `references/project-profiles.md` and add a **Generic** default profile so the skill applies to any repository.
- Generalize `references/issue-risk-contract.md`: core risk packs and mandatory expanded-triggers are now project-agnostic (added Auth/permissions/secrets and Concurrency/shared-state/ordering); domain packs (geospatial/CRS, time-series/forcing, numerical/conservation, solver/threading) and domain triggers move into the profile that contributes them.
- Phase 0 step 0 now selects a profile from `project-profiles.md` (Generic when none matches) instead of assuming a SHUD project family; fixture templates carry `Project profile: Generic|...`.
- No behavior change for SHUD-family repos: SHUD/rSHUD/AutoSHUD remain first-class profiles with full fidelity.

## [0.2.0] - 2026-06-01
- Add Phase 4.5 independent finding verification gate, borrowing the find/verify separation from the `code-review` skill: Phase 4 reviewers now emit candidate findings, and a separate codeagent verifier adjudicates each deduped candidate as CONFIRMED/PLAUSIBLE/REFUTED before Phase 5.
- Make verification recall/precision bias follow the fixture level (`high`/`broad-expanded` keep PLAUSIBLE merge-blocking; `low`/`compact` block only on CONFIRMED); REFUTED candidates are dropped with recorded rationale and do not count toward the review round budget.
- Add a removed-behavior auditor checklist item to the Integration reviewer and a dedup step ahead of verification.
- Add the Phase 4.5 verifier template to `references/phase-4-cross-review.md`.
- Slim `SKILL.md`: collapse 16 review/fix-governance Core Rules (round budgets, hard gates, escalation, CI-only, large-PR staging) into 3 navigational non-negotiables that defer mechanics to `references/phase-flow.md`; always-loaded skill doc drops from 32 to 18 Core Rules (~20KB to ~13KB) with no loss of executable detail.
- De-duplicate review semantics: `risk-adaptive-cross-review` is now the canonical source for reviewer packs, the finding contract, and failure-class synthesis. This workflow references those definitions instead of hand-forking them; `references/phase-4-cross-review.md` is declared the codex/OpenSpec instantiation only.

## [0.1.0] - 2026-05-25
- Initial canonical package for the Codex + codeagent issue workflow.
- Adds OpenSpec fixture gates, risk-adaptive review/fix loops, parallel worktree delegation rules, PR evidence, CI, and human-gated merge guidance.
- Documents relationship to `risk-adaptive-cross-review` and upstream `stage-change-pipeline`.
- Documents reuse points for `implementation-planning`, `review`, `entropy-review`, `git-worktree-workflows`, and `project-documentation`.
- Keeps requirements clarification and issue-readiness checks out of this automated implementation flow; `stage-change-pipeline` owns those upstream.
