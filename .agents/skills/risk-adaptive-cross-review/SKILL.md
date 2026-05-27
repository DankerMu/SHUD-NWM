---
name: risk-adaptive-cross-review
description: >
  Run multi-perspective risk-adaptive reviews for PRs, diffs, branches, OpenSpec
  changes, or stage-change artifacts. Use when the user asks for a risk-adaptive
  review, multi-review, parallel review, cross-review, high-risk PR review, or
  wants reviewers split across correctness, integration, security/performance,
  test evidence, spec compliance, or invariant/state-machine compatibility. Do
  not use for ordinary quick review unless the user asks for multi-perspective
  or risk-adaptive depth.
version: 0.1.0
---

# Risk-Adaptive Cross Review

Run review as a small review system rather than a single pass. Select reviewer
packs by artifact type and risk, launch independent reviews when tooling allows,
then synthesize findings by failure class so fixes close invariants instead of
chasing isolated comments.

## Route First

Choose one mode:

| Mode | Use when | Primary artifact |
| --- | --- | --- |
| **PR Review** | PR, diff, branch, staged changes, or implementation artifact | Code/test/config/docs changes |
| **OpenSpec Review** | OpenSpec change, stage-change pipeline, proposal/design/specs/tasks | Spec and issue-planning artifacts |
| **Hybrid Review** | A PR must be checked against OpenSpec or design docs | Both implementation and fixture |

If the request is a simple quick code review, use `review` instead. If the
request is only consistency/drift-focused, use `entropy-review`.

## When Not to Use

- Do not use for ordinary quick review, small diffs, or style-only feedback; use
  `review`.
- Do not use for consistency-only or entropy-only checks; use `entropy-review`.
- Do not use to implement fixes, update code, or merge PRs; use the appropriate
  implementation workflow.
- Do not use to turn a design stage into GitHub issues; use `stage-change-pipeline`.

## Risk Triage

Classify the review before launching reviewers:

- **Low**: small local change, no public contract, no data/security/CI behavior.
- **Medium**: multiple files or modules, test behavior, public-ish API, shared helper.
- **High**: auth/permissions, file IO/path safety, DB-backed state, migrations,
  retries/cancellation, publish/delete/rollback, schema/evidence contracts,
  production config, money, security, data loss, shared state machines.

High-risk reviews require an explicit invariant lens. Do not treat a patch as
safe just because each cited line looks fixable.

## Reviewer Packs

Read [reviewer-packages.md](references/reviewer-packages.md) to select the
reviewer set. Defaults:

- PR Review low/medium: Correctness, Integration, Security/Performance, Test &
  Evidence Coverage.
- PR Review high: add Spec Compliance when a fixture exists, plus
  Invariant/State-Machine/Compatibility.
- OpenSpec Review: Design Consistency, Spec Completeness, Tasks Executability.

Use `codeagent-wrapper --parallel --full-output --backend codex` when multiple
independent reviewers can run concurrently. If that tool is unavailable, run the
same reviewer prompts serially and report the limitation.

## Finding Contract

Read [finding-contract.md](references/finding-contract.md) before synthesizing.
Treat a finding as merge-blocking only when it names severity, failure class,
violated contract/invariant, concrete scenario, evidence, required fix direction,
required test or proof, sibling surfaces to audit, and blocking status.

Vague concerns are notes unless you can independently turn them into the full
contract from the artifact and fixture.

## Synthesis

After reviewers return, read [failure-class-synthesis.md](references/failure-class-synthesis.md)
and produce:

- reviewer coverage and risk level
- deduplicated P0/P1/P2 findings grouped by failure class
- invariant or sibling-surface audits required before the next review
- fix checklist written as class-level closure tasks
- whether another cross-review is warranted after fixes

Do not ask implementers to fix each cited line independently when multiple
findings share the same root invariant.

## Output

Return concise structured output:

```markdown
## Risk-Adaptive Cross Review

**mode**: pr-review | openspec-review | hybrid-review
**risk**: low | medium | high
**reviewers**: <reviewer names>
**verdict**: approve | request-changes | needs-discussion

### Findings
- **P0/P1/P2** `<failure-class>`: <issue, evidence, consequence, fix direction>

### Fix Groups
- `<failure-class>`: <class-level closure task and verification>

### Notes
- <non-blocking observations, unresolved limits, or tool constraints>
```

## Boundaries

- Never modify source files as part of the review.
- Do not run nested AI delegation from inside delegated reviewer prompts.
- Do not approve high-risk work when the governing invariant has not been
  stated or tested.
- Use `stage-change-pipeline` for turning design stages into issues.
- Use `codex-codeagent-workflow` for implementing a GitHub issue end to end.
