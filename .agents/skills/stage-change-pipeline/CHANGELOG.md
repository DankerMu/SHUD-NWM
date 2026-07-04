# Changelog

All notable changes to this skill will be documented in this file.
This project adheres to [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.10.0] - 2026-07-02

Closes the audit finding that the hardened Stage 3→5.5 entry point silently bypassed the documented grill gate: the multi-round `grill-me` stress-test lived only in Stage 1 prose ("可选但推荐"), was absent from all workflow scripts, and — being user-interactive — cannot run inside a workflow at all. In practice it never fired.

### Added
- **Breaking: grill-gate attestation** in `full-pipeline.workflow.js`: a required `grillGate` arg — `"passed"` (grill-me ran in the main conversation) or `"skipped:<reason>"` (explicit, reasoned skip). Missing or malformed → the script refuses to start. Since workflow subagents cannot interact with the user, the gate is enforced as a launch precondition rather than an in-script step.
- `grill_gate` field in `full-pipeline`'s returned `logEntry`, so the pass/skip decision (with reason) lands in `docs/stage-pipeline-log.jsonl` and skip-rate becomes auditable. `review-loop` standalone runs operate on an existing change (the gate decision is upstream) and do not carry the field.

### Changed
- SKILL.md Stage 1 压测门禁 upgraded from "可选但推荐" to an EITHER/OR gate (run `grill-me` OR record an explicit skip reason), stating explicitly that the multi-round grill can only happen in the main conversation before the workflow launches; invocation examples, the logEntry schema, and the dependency list now include `grillGate`/`grill_gate`.

## [0.9.0] - 2026-07-02

Hardening pass from a defect audit of the workflow scripts and `SKILL.md`. **Minor** bump because it adds a new exit gate (completion self-audit) and a returned `logEntry` contract.

### Added
- **Completion self-audit gate** in both `review-loop.workflow.js` and `full-pipeline.workflow.js`: when the fix-verify loop clears all findings, a self-audit subagent re-derives the Stage 1 goals/acceptance criteria from the design docs, confirms each maps to a spec requirement and a task, and runs `openspec status` expecting 4/4. Pass exits the loop; fail turns the gaps into active findings that keep looping toward `MAX_ROUNDS` (residual on cap). Enforces the previously documented-but-unenforced SKILL.md 4-condition exit.
- **`logEntry` return object** from both loop scripts carrying the accountability-log fields (`gate_net_catch`, `p0`, `p1`, `regressions`, `approx_subagent_calls`, `verdict`) computed from real per-run counters. SKILL.md's "跨运行问责" section now spells out the orchestrator handoff as explicit numbered steps (take `logEntry`, add `date`, append one JSON line to `docs/stage-pipeline-log.jsonl`, commit) and aligns the documented schema to exactly what the scripts return.
- `failureClass` and `impact` are now **required** finding fields in `FINDINGS_SCHEMA`, and all six reviewer prompts request them, matching the SKILL.md finding contract.

### Changed
- **Ack tokens now match the mandated strings**: scripts emit `Stage 4.5 round <N>: started` on round start and `round <N> verdict: <resolved>/<total> resolved, residual P0=<n> P1=<n>` after verdicts, as SKILL.md requires for auditable proof the gate ran.
- **`gate_net_catch` definition unified** across prose and code: per round it counts findings the fixer claimed resolved but the verifier judged `unresolved`/`regressed` **plus** newly-introduced regressions (regressions were never counted before).
- Stage 5 issue template and the issue-creation prompt emit one `Depends on #NN` line per dependency (was `**Dependencies:** #a, #b`) so the downstream `subagent-workflow` DAG reader can parse the dependency graph.
- `full-pipeline` args contract unified to `{changeName, designDocs, stageLabel?}` in both SKILL.md examples; the dead `skillDir` arg is removed.
- SKILL.md now states vague/unanchored review items are **rejected** (not "降级为 note"), matching actual code behavior.
- Added `// NOTE: duplicated in … — keep in sync` comments above the reviewer briefs, `FINDINGS_SCHEMA`, `VERDICT_SCHEMA`, and `ALIGNMENT_SCHEMA` blocks that are copied across the scripts and SKILL.md.

### Fixed
- **Reviewer success floor**: fewer than 2 of 3 reviewers returning no longer reads as a clean review — the pipeline logs the failure and aborts with `verdict: "review-round-failed"` instead of proceeding on partial/empty results.
- **Alignment review null-guard**: a null alignment review now retries once and then aborts with an explicit error, instead of silently reporting the issues as "aligned" (both `full-pipeline.workflow.js` and `issue-alignment.workflow.js`).
- **`stageLabel` enforcement**: when a stage label is provided, the creation prompt now instructs `gh label create "<stageLabel>" --force` and applying that label to the Epic and every sub-issue, so Stage 5.5's label filter no longer silently drops unlabeled sub-issues as falsely "aligned".
- **Oracle integrity for fixers**: every fixer prompt now states the design docs / implementation plan / Stage 1 criteria (plus, for alignment fixers, the change artifacts) are an immutable oracle that must never be edited to make a finding or issue pass.
- **Defensive args parsing** added to `review-loop.workflow.js` (guards `changeName`) and `issue-alignment.workflow.js` (guards `changeName` and `epicNumber`), matching `full-pipeline.workflow.js`.
- **Truncation guard**: the `gh issue list --limit 100` step now instructs the agent to treat a full-100 result as likely truncated and re-list with a higher limit.

## [0.8.2] - 2026-07-02
- Add English trigger phrases to the SKILL.md description ("start the next stage", "turn this design into issues", "create issues from the spec/design", "run the stage pipeline"): the trigger surface was almost entirely Chinese, so English prompts rarely auto-activated the skill.
- Fix a typo in the `skill.json` description ("an reviewed" → "a reviewed").

## [0.8.1] - 2026-06-22
- **Fix**: Rewrite `full-pipeline.workflow.js` to inline all review-loop and issue-alignment logic instead of using `workflow()` sub-calls. Fixes "nesting is limited to one level" error when the script is invoked via `Workflow()` (the `workflow()` calls to child scripts created an implicit second nesting level). `review-loop.workflow.js` and `issue-alignment.workflow.js` remain available as standalone scripts.
- **Fix**: Add defensive args parsing — if `args` is passed as a JSON string instead of an object (caller-side serialization bug), parse it; abort early with clear error if `changeName` is missing.

## [0.8.0] - 2026-06-20
- **Breaking**: P1 升为阻塞带——P0 和 P1 均驱动回环，不再允许 P1 携带到 issue 而跳过修复。回环退出条件从"P0 清零 + P1 resolved-or-carried"变为"P0 + P1 全部 resolved"。
- Add `review-loop.workflow.js`: 将 Stage 3→4→4.5 回环逻辑从散文指令改为 Claude Code Workflow 脚本硬编码执行。`while (activeFindings.length > 0 && round < MAX_ROUNDS)` 循环不可被编排器跳过，解决实际运行中"一轮 review 完没 clean 就跳到 Stage 5"的问题。
- Workflow 脚本输出 `gateNetCatch` 指标，可直接写入跨运行问责日志。
- Add Stage 5.5 `issue-alignment.workflow.js`: Issue 创建后强制运行 Issue-Change 对齐审核，检查覆盖完整性、模块边界、依赖链、Scope 准确性、引用和内容漂移，fix-verify 循环最多 2 轮，解决"Issue 和 change 没有完全对齐"的问题。
- **(dim 5)** Add resolved-signature tracking and convergence detection to both workflow scripts: maintain a `resolvedSignatures` set across rounds; when a regression matches a previously-resolved title, flag it as whack-a-mole and log it; if active finding count stops decreasing past round 1, break early to avoid empty churn. Return `whackAMoleCount` in review-loop results.
- **(dim 8)** Add `full-pipeline.workflow.js`: chains review-loop → issue creation → issue-alignment via `workflow()` sub-calls, reducing the trigger surface from 2 manual workflow invocations to 1. Add a "触发锚定" section to SKILL.md with guidance on anchoring the single invocation to CI/pre-PR hooks in consuming repos and auditing skip-rate.

## [0.7.0] - 2026-06-18
- Stage 3 review contract now draws the finding's failure type from the `risk-adaptive-cross-review` `finding-contract.md` Failure-Class Vocabulary (including the spec classes `design-consistency`/`spec-completeness`/`task-executability`) and applies its Reject precision gate so vague, unanchored, or style-only items become notes instead of P0/P1.
- Add an **Oracle Integrity** constraint to the Stage 4.5 verification gate: the source design docs, implementation plan, and Stage 1 stage goals/acceptance criteria are the immutable oracle; fixes edit the OpenSpec change, never the artifacts that judge it. A genuine source-design change routes back through `brainstorming`/`future-aware-architecture` and is recorded, never silently rewritten to clear a finding.
- Add a **completion self-audit (premature-completion guard)** as a Stage 4.5 exit precondition: before entering Stage 5, re-derive each Stage 1 stage goal and acceptance criterion and confirm every requirement maps to a spec requirement plus task with no dropped design goal/boundary and no internally contradictory fix — an uncovered requirement returns to Stage 4 instead of passing on "looks done". Adapted from stellarlinkco/skills `harness`/`code-review` (clean-room, with author permission).

## [0.6.0] - 2026-06-18
- Add a cross-run "Loop Accountability" section (meta-loop dimension 6): a committed, append-only catch-rate log (`docs/stage-pipeline-log.jsonl`) records per-run `gate_net_catch` (findings the independent Stage 4.5 verifier overturned that Stage 3 review and `openspec status` missed), rounds, residuals, regressions, and cost.
- Define a kill criterion (≥5-run minimum sample; retire or narrow the gate if `gate_net_catch` ≈ 0) and a ratchet rule (promote a finding-class recurring across ≥2 runs into a permanent openspec/lint/CI check), so the gate keeps earning its cost and resolved issues become invariants.
- Scope explicitly to organizational accountability only — no Reflexion-style cross-change learning — to avoid importing bias across unrelated changes.

## [0.5.0] - 2026-06-18
- Harden Stage 4.5 trigger reliability (meta-loop dimension 8): forbid skipping straight from Stage 4 to Stage 5, require per-round acknowledgement tokens (`round <N>: started` / `verdict ...`) as auditable proof the gate ran, and honestly document that a portable doc-skill cannot install a host-repo pre-commit/pre-PR hook — per-project skip-rate stays an audited open question.
- Rework severity gating (meta-loop dimensions 1 + 4): P0 is the blocking band that drives the loop; P1 becomes a non-blocking carry band — still fixed opportunistically each round but no longer blocks exit or forces extra rounds. Exit gate is now "P0 cleared + P1 resolved-or-carried-to-issue", removing the prior "until clean" non-convergence smell.

## [0.4.0] - 2026-06-18
- Replace the single review→fix pass with a bounded verification loop: Stage 4 now resolves both P0 and P1 findings, and a new Stage 4.5 independent verification gate adversarially confirms each finding is resolved (default `unresolved` without evidence) and delta-scans for regressions in touched artifacts.
- Loop back to Stage 4 while any P0/P1 stays `unresolved`/`regressed`, capped at 3 rounds; on cap or convergence, residual findings are recorded into the issue body with a `needs-followup` marker instead of being silently dropped.
- Add the `verifier` subagent (`verify-review-fixes`) as a Stage 4.5 dependency that must not be the fixer; update pipeline diagram, skip strategy, and quick reference accordingly.

## [0.3.0] - 2026-06-15
- Add `grill-with-docs` as the Stage 2 domain-modeling pass: align terminology against `openspec/glossary.md` and persist long-lived decisions to `docs/adr/` while writing design/specs.

## [0.2.0] - 2026-06-15
- Integrate the `grill-me` skill as an optional pre-OpenSpec design stress-test gate between Stage 1 and Stage 2.
- Migrate Stage 3 review execution from the `codeagent`/`codeagent-wrapper` CLI to the orchestrator's native parallel subagents (`reviewer`), aligning with `subagent-workflow`. Drop `codeagent-wrapper` from requirements and the `codeagent` tag.
- Rename stale references from `codex-codeagent-workflow` to `subagent-workflow` (dependency note, When-Not-To-Use, implementation-ready contract, quick reference).

## [0.1.0] - 2026-05-25
- Initial canonical package for the stage-change pipeline.
- Converts design-stage context into OpenSpec changes, parallel Codex review, fixes, and fine-grained GitHub issues.
- Includes small-PR issue splitting rules based on module, ownership, dependency, and verification boundaries.
- Documents how Stage 3 aligns with `risk-adaptive-cross-review` OpenSpec review semantics.
- Documents reuse points for `clarify`, `future-aware-architecture`, `implementation-planning`, `project-documentation`, and `gh-create-issue`.
- Adds an Implementation-Ready Issue Contract so downstream implementation workflows should not need requirements clarification.
- Requires `Implementation Ready: yes` on generated sub-issues before they can enter `codex-codeagent-workflow`.
