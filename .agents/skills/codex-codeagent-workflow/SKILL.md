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
- `risk-adaptive-cross-review` is the canonical source of review vocabulary (reviewer packs, risk triage, the finding contract, failure-class synthesis). This workflow consumes those definitions rather than forking them; use the standalone skill directly when the user wants a risk-adaptive review outside Phase 0-8.
- Use `review` for focused artifact review when the work does not justify multi-perspective cross-review, and `entropy-review` when a follow-up check is specifically about consistency, naming drift, error-model splits, or pattern duplication.
- Use `git-worktree-workflows` only for user-facing worktree guidance or recovery; this workflow's parallel code-writing isolation remains governed by `references/parallel-worktree-delegation.md`.
- Use `project-documentation` when implementation changes require docs-set refresh, docs drift checks, or source-of-truth cleanup outside the PR evidence summary.
- Within this workflow, Phase 4 and follow-up review rounds use `references/phase-4-cross-review.md`. That reference is only the codex/OpenSpec instantiation — concrete `codeagent-wrapper` task scaffolding, the `Invariant Matrix` binding, and the Phase 4.5 verifier. Reviewer-pack scope and the finding contract themselves are not redefined there; they come from `risk-adaptive-cross-review`.

## Core Rules

- **OpenSpec change is mandatory**: Every implemented issue must have `openspec/changes/<change>/{proposal.md,design.md,tasks.md}` plus required spec deltas. If missing, Codex creates and fills it before implementation.
- **OpenSpec is the fixture**: Risk triage, must-preserve behavior, selected/not-selected risk packs with reasons, evidence mapping, and non-goals belong in the OpenSpec change.
- **Project profile is project-local and living**: The active risk profile lives at `openspec/project-profile.md`, not inside this skill. Phase 0.0 bootstraps it on first run by scanning the repo (Generic when nothing project-specific is found); Phase 0.5 updates it when an issue exposes a new risk surface. The shared `references/project-profiles.md` holds only the Generic default and reusable example templates (SHUD/rSHUD/AutoSHUD). Never hand-fork project-specific surfaces into the shared skill.
- **Fixture review is mandatory**: Every OpenSpec change must pass one focused `codeagent-wrapper --backend codex` read-only fixture review before implementation, then `openspec validate <change> --strict --no-interactive`.
- **Codex may edit specs, not implementation**: Codex may directly create/edit `openspec/changes/<change>/**`. Source, runtime tests, configs, and PR templates go through `codeagent-wrapper --backend codex` unless the user explicitly overrides.
- **Serial execution**: Process one issue through Phase 0-8 before starting another.
- **Merge gate**: Phase 8 merge is human-gated by default. If the user explicitly pre-authorizes auto-merge for the run, merge after final review is clean and required CI passes, then continue to the next unblocked issue.
- **Self-repair by delegation**: Build, lint, test, review, fixture review, OpenSpec validation, and CI failures become precise codeagent or spec-fix tasks. Continue fix/review rounds until the latest comprehensive cross-review is clean, unless an ordinary-loop gate in `references/phase-flow.md` requires retro, invariant inventory, refactor/redesign, PR split, or a scope decision.
- **Review vocabulary is canonical in `risk-adaptive-cross-review`**: Reviewer packs, risk-triage levels, the actionable finding contract, and failure-class synthesis are owned by the `risk-adaptive-cross-review` skill. Do not restate or hand-fork them here. This workflow only binds them to OpenSpec fixture levels, the `Invariant Matrix`, and the codeagent task scaffolding in `references/phase-4-cross-review.md`.
- **Reviewers produce candidates; verify before fix**: Phase 4 reviewers emit candidate findings only. Before Phase 5, dedup near-duplicates and run one independent `codeagent-wrapper --parallel --backend codex` verifier per candidate — never its originating reviewer, never Codex self-judging — returning CONFIRMED, PLAUSIBLE, or REFUTED. Only CONFIRMED plus risk-weighted PLAUSIBLE enter Phase 5; REFUTED is dropped with a one-line recorded rationale and is not a cross-review round. Bias by fixture level: `high`/`broad-expanded` keep PLAUSIBLE merge-blocking; `low`/`compact` block only on CONFIRMED. Mechanics live in `references/phase-flow.md` Phase 4.5.
- **Risk-adaptive review/fix governance**: Repair intensity, six-reviewer high-risk escalation, the Phase 0.5 `Invariant Matrix`, pattern escalation and its freeze, invariant closure over finding chase, fix-by-failure-class grouping, the review round budget, the five-round hard gate and post-five budget, round-counter scope, the working-day retro trigger, CI-only repair bypass, and large-PR shared-boundary staging are all governed by `references/phase-flow.md` and `references/issue-risk-contract.md`. Two non-negotiables that override any reference detail: never run a sixth comprehensive cross-review round without first persisting the Gate-Level PR Strategy Review package, and never reset the round counter across commits, CI-only fixes, or sibling surfaces under the same PR. Keep working at the invariant level instead of chasing isolated findings; escalate to the user only for real blockers or a scope/product decision not derivable from the fixture.
- **Parallel codeagent execution default**: When two or more delegated codeagent tasks can run concurrently, prefer `codeagent-wrapper --parallel --backend codex` over manually launching separate wrapper processes. Use serial execution only for a true dependency chain, fixture repair that must inspect the prior result, or a tooling failure that makes parallel mode unavailable.
- **Parallel code-writing isolation**: Parallel code-writing tasks in Phase 1 implementation or Phase 6 fixes are allowed only through `references/parallel-worktree-delegation.md`. Codex must persist a parallel worktree manifest, assign disjoint allowed write sets, use separate git worktrees under `.codex/worktrees/`, reject out-of-scope worker diffs, integrate patches only from the parent PR worktree, and clean or explicitly retain every delegated worktree before finishing the PR. CI-only repairs should stay serial and minimal; if a CI failure needs parallel code-writing, reclassify it as semantic or normal Phase 5/6 work instead of using the CI-only bypass.
- **Escalate only when stuck**: Missing `codeagent-wrapper`/`openspec`, inaccessible issue inputs, repeated delegated failure, OpenSpec validation that cannot be made green, CI infrastructure failure, or merge decision without explicit auto-merge pre-authorization.
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
Phase 4: risk-adaptive codeagent cross-review (produces candidate findings)
Phase 4.5: independent codeagent verification gate (CONFIRMED/PLAUSIBLE/REFUTED, recall-biased by risk)
Phase 5: Codex synthesizes fix checklist from verified findings
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
3. `references/issue-risk-contract.md`: fixture levels, core risk packs, and triage requirements.
   - `references/project-profiles.md`: shared template catalog (Generic default plus SHUD/rSHUD/AutoSHUD examples); the active profile is project-local at `openspec/project-profile.md`, bootstrapped in Phase 0.0.
4. `references/phase-4-cross-review.md`: cross-review prompt structure and the Phase 4.5 verifier template.
5. `references/parallel-worktree-delegation.md`: required mechanics for any parallel code-writing delegation.
6. `risk-adaptive-cross-review` skill: canonical review vocabulary (reviewer packs, finding contract, failure-class synthesis) that this workflow consumes, not forks.

If a reference appears to conflict with a Core Rule, the Core Rule wins and the reference should be corrected before continuing. For reviewer-pack scope and the finding contract specifically, the `risk-adaptive-cross-review` definition is canonical; align this workflow to it rather than editing a local fork.

## References

- `references/phase-flow.md`: detailed Phase 0-8 execution, prompts, evidence, and merge gate.
- `references/issue-risk-contract.md`: fixture levels, core risk packs, mandatory expanded triggers, OpenSpec fixture templates, and fixture review prompt (project-profile-agnostic).
- `references/project-profiles.md`: shared template catalog of profiles (Generic default plus SHUD/rSHUD/AutoSHUD examples); the active per-project profile lives at `openspec/project-profile.md` and is bootstrapped in Phase 0.0, maintained in Phase 0.5.
- `references/phase-4-cross-review.md`: reusable codeagent parallel review template and the Phase 4.5 independent verifier template.
- `references/parallel-worktree-delegation.md`: required worktree isolation, manifest, integration, and cleanup rules for parallel implementation/fix tasks.

Related skills:

- `risk-adaptive-cross-review`: canonical owner of review semantics (reviewer packs, finding contract, failure-class synthesis); this workflow binds them to OpenSpec fixtures and codeagent delegation.
- `stage-change-pipeline`: upstream design-stage-to-issue workflow.

## When Not to Use

- Documentation-only or spec-only PRs without implementation.
- Emergency hotfixes that intentionally skip review.
- Unresolved upstream dependencies that make implementation impossible.
