---
name: codex-codeagent-workflow
description: >
  End-to-end GitHub issue implementation workflow for Codex orchestration with codeagent-wrapper using the Codex backend. Covers issue selection, mandatory OpenSpec change fixture creation/review, risk-adaptive implementation review, verification, PR evidence, Chinese work-summary comment, CI, and default human-gated merge with explicit user pre-authorization for auto-merge. Use when the user wants to implement a GitHub issue, process the next DAG issue, run the workflow, or says triggers such as "implement #XX", "do the next issue", "run the workflow", "codex-codeagent", "处理下一个issue", "跑工作流", "开始实现", "下一个该做什么", "do it", or "开干". Do NOT use for documentation-only/spec-only work without implementation, intentional emergency hotfixes that skip review, or pure brainstorming.
---

# Codex + codeagent Issue Workflow

Codex orchestrates the issue workflow. Code/test/config implementation, fix passes, and code reviews are delegated to `codeagent-wrapper --backend codex`. Codex owns issue selection, OpenSpec fixture authoring, local verification, review synthesis, git/PR operations, CI tracking, and the final merge gate.

## Prerequisites

- `codeagent-wrapper` available in `PATH` or at `$HOME/.claude/bin/codeagent-wrapper`.
- `openspec` CLI available in `PATH`.
- `gh` CLI authenticated.
- `git`.
- Project build/test toolchain.

Read the `codeagent` skill before the first `codeagent-wrapper` invocation for current syntax and timeout guidance.

## Upstream Contract

This workflow assumes every input issue is already implementation-ready. When issues are produced by `stage-change-pipeline`, that upstream flow owns scope clarity, acceptance criteria, product decisions, module boundaries, dependencies, and expected PR boundary. This workflow does not perform requirements clarification, issue-readiness checks, or product-scope negotiation during automated implementation runs.

## Supporting Skills

- Use `stage-change-pipeline` upstream when the user needs to turn design-stage documents into reviewed OpenSpec changes and fine-grained GitHub issues before implementation begins.
- Use `implementation-planning` only as a non-interactive execution-strategy aid when the accepted issue/OpenSpec fixture is clear but the implementation path needs staged rollout, rollback, dependency ordering, or PR split strategy. Do not use it to discover or negotiate product scope.
- Use `risk-adaptive-cross-review` as the standalone review contract when the user asks to run a risk-adaptive PR review outside this full workflow, or when you need the reviewer-pack/finding-contract guidance without executing Phase 0-8.
- Use `review` for focused artifact review when the work does not justify multi-perspective cross-review, and `entropy-review` when a follow-up check is specifically about consistency, naming drift, error-model splits, or pattern duplication.
- Use `git-worktree-workflows` only for user-facing worktree guidance or recovery; this workflow's parallel code-writing isolation remains governed by `references/parallel-worktree-delegation.md`.
- Use `project-documentation` when implementation changes require docs-set refresh, docs drift checks, or source-of-truth cleanup outside the PR evidence summary.
- Within this workflow, Phase 4 and follow-up review rounds remain governed by `references/phase-4-cross-review.md`; keep that reference aligned with `risk-adaptive-cross-review` concepts such as reviewer packs, actionable finding contracts, and failure-class synthesis.

## Core Rules

- **OpenSpec change is mandatory**: Every implemented issue must have `openspec/changes/<change>/{proposal.md,design.md,tasks.md}` plus required spec deltas. If missing, Codex creates and fills it before implementation.
- **OpenSpec is the fixture**: Risk triage, must-preserve behavior, selected/not-selected risk packs with reasons, evidence mapping, and non-goals belong in the OpenSpec change.
- **Fixture review is mandatory**: Every OpenSpec change must pass one focused `codeagent-wrapper --backend codex` read-only fixture review before implementation, then `openspec validate <change> --strict --no-interactive`.
- **Codex may edit specs, not implementation**: Codex may directly create/edit `openspec/changes/<change>/**`. Source, runtime tests, configs, and PR templates go through `codeagent-wrapper --backend codex` unless the user explicitly overrides.
- **Serial execution**: Process one issue through Phase 0-8 before starting another.
- **Merge gate**: Phase 8 merge is human-gated by default. If the user explicitly pre-authorizes auto-merge for the run, merge after final review is clean and required CI passes, then continue to the next unblocked issue.
- **Self-repair by delegation**: Build, lint, test, review, fixture review, OpenSpec validation, and CI failures become precise codeagent or spec-fix tasks. Continue fix/review rounds until the latest comprehensive cross-review is clean, unless an ordinary-loop gate below requires retro, invariant inventory, refactor/redesign, PR split, or a scope decision.
- **Risk-adaptive hard gates**: Do not apply the same repair intensity to every issue. Low-risk compact changes may use focused fixes. Medium-risk changes trigger pattern escalation after the second same-class finding. High-risk or expanded changes involving file IO/path safety, auth/permissions, evidence chains, publish/delete/rollback, production config, money, security, data loss, or shared helpers trigger invariant-surface inventory on the first critical/major reusable-pattern finding.
- **Six-review high-risk escalation**: Phase 4 uses the standard 4 reviewers for ordinary expanded work, but high or broad-expanded PRs that touch DB-backed state, retry/cancellation, publish/delete/rollback, schema/evidence contracts, security boundaries, production config, or shared helper/state-machine roots must run 6 reviewers: Spec Compliance, Correctness, Integration, Security/Performance, Test & Evidence Coverage, and Invariant/State-Machine/Compatibility. Follow-up rounds use the same reviewer mix unless the fixture is revised to narrow the risk.
- **Front-load high-risk invariants**: For high-risk or broad-expanded changes, do not wait for review failures to discover the governing invariant. Phase 0.5 must define a compact `Invariant Matrix` before implementation, covering the end-to-end identity/contract that must propagate across producers, validators, storage/cache, public routes/entrypoints, failure paths, evidence, and downstream consumers. Phase 1 implementation and Phase 4 review must use this matrix as an acceptance fixture.
- **Pattern escalation freeze**: When pattern escalation triggers, Codex must stop ordinary line-item repair and must not start another cross-review until an `Invariant Surface Inventory` and `Regression Matrix` exist, the next Phase 6 prompt uses them, local verification passes, and Phase 6.2 reports the invariant clean.
- **Pattern escalation**: If two review/fix rounds surface findings in the same selected risk pack, failure class, helper pattern, or sibling surface family, or one high-risk major/critical finding exposes a reusable unsafe pattern, Codex must stop issuing narrow line-item fix prompts. The next Phase 6 pass must define the cross-cutting invariant, audit all analogous code paths, fix the invariant across the affected surface, and add matrix regression tests before another cross-review.
- **Invariant closure over finding chase**: For expanded fixtures and high-risk packs, a fix is not complete merely because the cited line is patched. Codex must verify the invariant across sibling modules, shared helper roots, entrypoints, read/write/staging surfaces, evidence producers/consumers, stale-state boundaries, failure paths, and unchanged downstream consumers named by the OpenSpec fixture.
- **Actionable review finding contract**: Treat review comments as merge-blocking only when they name severity, failure class, violated invariant or contract, concrete failing scenario or reproduction path, required test/evidence, sibling surfaces to audit, and merge-blocking status. Vague concerns without a concrete scenario/test become non-blocking notes unless Codex can independently turn them into the full contract from the diff and fixture.
- **Fix by failure class, not by comment**: Phase 5 must merge findings with the same failure class into one fix group. Phase 6 must delegate one class-level closure task per group, not one prompt per cited line, and must require tests or evidence for the class-level behavior.
- **Review round budget**: Round 1 findings are normal. If Round 2 repeats the same failure class, trigger invariant closure before another review. If Round 3 still repeats the same failure class, stop ordinary fix/review loops and perform a Review Failure Retro before continuing; identify whether the fixture was under-scoped, the fix prompt was too narrow, the reviewer contract was vague, or the PR must be split/scoped with the user.
- **Five-round hard gate**: If a PR reaches 5 comprehensive cross-review rounds total, Codex must stop ordinary fix/review cycling immediately, even if findings are not repeated verbatim. This is a workflow transition, not workflow termination. Before any further codeagent fix, cross-review, Phase 7 final review, CI wait-for-merge, or merge action, Codex must produce and persist a Deep Review Failure Retro plus a Gate-Level PR Strategy Review, Invariant Surface Inventory, and Regression Matrix. The strategy review must explicitly decide whether the PR direction is wrong, whether a refactor or architecture change is needed, whether the loop is only chasing symptoms, and whether the implementation fundamentally closes the required functionality and security/safety invariants. The next action must be one of: update the OpenSpec fixture/invariant matrix, redesign/refactor the implementation around the root invariant, run one cross-cutting invariant closure over all sibling surfaces, split/scope the PR within the existing issue/OpenSpec boundary, ask the user only for a scope/product decision that cannot be derived from the fixture, or downgrade a reviewer pattern with explicit contract-based rationale. It is forbidden to run "one more similar review" or issue another narrow cited-line fix after round 5.
- **Post-five review budget**: After the five-round hard gate is tripped and the retro/inventory-driven corrective action is completed, Codex may run at most one comprehensive cross-review to verify closure. If that review reports any critical/major finding in the same invariant family, do not continue to rounds 6, 7, 8, etc. by default and do not return to narrow line-item repair. Re-enter the Gate-Level PR Strategy Review, update the gate package, and choose a stronger root-cause action such as redesign/refactor, fixture revision, PR split, or reviewer-pattern downgrade. Escalate to the user only when the next action requires a product/scope decision that cannot be resolved from the issue and OpenSpec fixture.
- **Round counter scope**: Count every comprehensive Phase 4 or follow-up cross-review on the PR, including "round N after fixes" and reruns at a new SHA. Do not reset the counter after commits, CI-only fixes, or a different sibling surface under the same PR.
- **Review failure retro trigger**: If a PR spends more than one working day in review/fix loops, or if reviewer findings keep moving to sibling surfaces under the same invariant, stop increasing ordinary rounds and run a Review Failure Retro immediately. The next action must change the fixture, invariant matrix, implementation scope, or reviewer prompt; it must not be "run another similar review".
- **CI-only repair bypass**: CI failures do not require cross-review when the fix is limited to formatting, deterministic test adjustment, CI wiring, dependency cache/install configuration, or log/evidence plumbing with no runtime/product behavior or test-meaning change. Verify locally where possible, push, and wait for CI. If the CI fix changes source behavior, public contracts, production config, security boundaries, or test assertions that alter product semantics, return to Phase 5-6 and rerun the appropriate review gate.
- **Large PR staging**: For broad expanded PRs that touch shared helpers plus multiple business lanes, front-load shared boundary hardening before business-lane fixes when practical. If the shared helper/root-cause surface emerges during review, pause business-lane patching and close the shared invariant first.
- **Parallel codeagent execution default**: When two or more delegated codeagent tasks can run concurrently, prefer `codeagent-wrapper --parallel --backend codex` over manually launching separate wrapper processes. Use serial execution only for a true dependency chain, fixture repair that must inspect the prior result, or a tooling failure that makes parallel mode unavailable.
- **Parallel code-writing isolation**: Parallel code-writing tasks in Phase 1 implementation or Phase 6 fixes are allowed only through `references/parallel-worktree-delegation.md`. Codex must persist a parallel worktree manifest, assign disjoint allowed write sets, use separate git worktrees under `.codex/worktrees/`, reject out-of-scope worker diffs, integrate patches only from the parent PR worktree, and clean or explicitly retain every delegated worktree before finishing the PR. CI-only repairs should stay serial and minimal; if a CI failure needs parallel code-writing, reclassify it as semantic or normal Phase 5/6 work instead of using the CI-only bypass.
- **Escalate only when stuck**: Missing `codeagent-wrapper`/`openspec`, inaccessible issue inputs, repeated delegated failure, OpenSpec validation that cannot be made green, CI infrastructure failure, or merge decision without explicit auto-merge pre-authorization.
- **Escalate repeated patterns**: When repeated patterns persist, keep working at the invariant level rather than chasing isolated findings. Escalate to the user only for real blockers, contradictory requirements, missing tooling, or a scope/product decision that cannot be resolved from the fixture.
- **Always use Codex backend**: Every `codeagent-wrapper` task uses `--backend codex` or a parallel task with `backend: codex`.
- **No nested AI delegation**: Delegated codeagent tasks are leaves. They must not invoke codeagent-wrapper, use this workflow/skills, spawn subagents, launch parallel agents, or ask another AI/code agent to implement, fix, review, or plan.
- **Silent long waits**: While waiting for `codeagent-wrapper` tasks or CI checks, prefer long quiet waits over short polling. Do not stream verbose watch output into the chat unless diagnosing a failure. Use long tool timeouts, sparse status checks, or quiet sleep loops that emit only final state or actionable failure summaries.
- **Chinese PR work summary**: Before merge gate or pre-authorized auto-merge, post a structured Chinese PR comment summarizing actual work, validation, review/fix closure, risks, and known limits.
- **Phase 8 dry-run before posting**: Generate PR body updates and evidence/work-summary comments into local files first, inspect their rendered markdown-sensitive content for shell quoting, stale findings, wrong SHA, and comment volume, then post with `--body-file`. Never construct multi-line PR comments with command substitution around untrusted markdown.

## Required Delegation Guard

Every codeagent prompt must include this guard, adapted only for grammar:

```text
Delegation boundary:
- You are a leaf codeagent task in a parent Codex workflow.
- Do not invoke codeagent-wrapper.
- Do not use the codeagent skill or codex-codeagent-workflow skill.
- Do not spawn subagents, parallel agents, nested reviewers, or any other AI/code agent.
- Do not ask another agent to implement, fix, review, or plan.
- Use ordinary shell/build/test tools and edit files directly within this assigned task.
- If the task cannot be completed without nested AI delegation, stop and report the blocker.
```

For fixture review tasks, replace the last two bullets with read-only wording from `references/issue-risk-contract.md`.

## Phase Skeleton

```text
Phase 0: select issue + discover/create OpenSpec change
Phase 0.5: embed risk triage into OpenSpec fixture + codeagent fixture review + openspec validate
Phase 1: codeagent implements + tests
Phase 2: Codex verifies only
Phase 3: Codex commits + opens PR
Phase 4: risk-adaptive codeagent cross-review
Phase 5: Codex synthesizes fix checklist
Phase 6: codeagent fixes
Phase 6.2: invariant audit for repeated/high-risk finding classes
Phase 6.5: repeat cross-review after fixes only while no ordinary-loop gate has triggered
Phase 7: independent final review after cross-review is clean
Phase 8: evidence + Chinese work summary + CI + merge gate or pre-authorized auto-merge
```

Load `references/phase-flow.md` when actively running the workflow.

## Execution Source

`SKILL.md` intentionally contains only trigger metadata, non-negotiable rules, and navigation. Do not duplicate detailed phase logic here. When actively running the workflow, load and follow `references/phase-flow.md` as the single source for Phase 0-8 steps, prompts, evidence templates, post-gate strategy path, and merge procedure.

Reference precedence:

1. `SKILL.md` Core Rules: non-negotiable constraints.
2. `references/phase-flow.md`: detailed execution steps and templates.
3. `references/issue-risk-contract.md`: fixture levels, project profiles, and risk-pack requirements.
4. `references/phase-4-cross-review.md`: cross-review prompt structure.
5. `references/parallel-worktree-delegation.md`: required mechanics for any parallel code-writing delegation.

If a reference appears to conflict with a Core Rule, the Core Rule wins and the reference should be corrected before continuing.

## References

- `references/phase-flow.md`: detailed Phase 0-8 execution, prompts, evidence, and merge gate.
- `references/issue-risk-contract.md`: SHUD/rSHUD/AutoSHUD project profiles, mandatory expanded triggers, risk-pack checklist, OpenSpec fixture templates, and fixture review prompt.
- `references/phase-4-cross-review.md`: reusable codeagent parallel review template.
- `references/parallel-worktree-delegation.md`: required worktree isolation, manifest, integration, and cleanup rules for parallel implementation/fix tasks.

Related skills:

- `risk-adaptive-cross-review`: independent PR/OpenSpec multi-review workflow and shared review semantics.
- `stage-change-pipeline`: upstream design-stage-to-issue workflow.

## When Not to Use

- Documentation-only or spec-only PRs without implementation.
- Emergency hotfixes that intentionally skip review.
- Unresolved upstream dependencies that make implementation impossible.
