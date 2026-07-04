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
invocation_posture: hybrid
version: 0.4.0
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

- **Low**: 3 or fewer files, a single module, no public/exported API, and no
  data/security/CI behavior.
- **Medium**: more than 3 files, more than one module, or any public/exported
  API; also test-behavior changes or a shared helper. This boundary is what makes
  the removed-behavior audit mandatory (see `reviewer-packages.md`), so classify
  it explicitly rather than by feel.
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

Beyond pack scope, every reviewer also applies the change-triggered **cross-cutting
lenses** in `reviewer-packages.md` — removed-behavior audit, wrapper/proxy
faithfulness, and altitude/ownership — whenever the diff trips a lens trigger.

Spawn the selected reviewers as parallel subagents through the orchestrator's
native mechanism (Claude Code: multiple Task calls in one message; Codex: parallel
subagents) when multiple independent reviewers can run concurrently. If parallel
subagents are unavailable, run the same reviewer prompts serially and report the
limitation. Each reviewer is a read-only leaf: it must not edit files, invoke this
skill, or spawn further nested subagents.

## Finding Contract

Read [finding-contract.md](references/finding-contract.md) before synthesizing.
Treat a finding as merge-blocking only when it names severity, a failure class
from the contract's Failure-Class Vocabulary, violated contract/invariant,
concrete scenario, evidence, consequence, required fix direction, required test
or proof, sibling surfaces to audit, and blocking status.

Apply the contract's Reject When precision gate so speculative, unanchored,
or style-only items become notes rather than blocking findings. Respect Oracle
Integrity: the spec/fixture, acceptance criteria, and existing tests are the
immutable oracle a finding is measured against — never propose weakening them to
clear a finding.

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

### Gap Sweep

Before issuing the verdict, run one final clean-slate pass over the artifact as a
fresh reviewer with the verified findings list visible, looking *only* for real
defects not already listed. Recall-biased first passes systematically miss:
removed behavior never re-established, caller/callee contract drift, boundary
values (empty, single, last, null, zero, unknown enum), error and cleanup paths,
async ordering and cancellation, cross-tenant/permission paths, migration/backfill
paths, cache invalidation, and wrapper recursion. Verify any new candidate through
the same standard and the Reject When gate; do not pad the list when the sweep
finds nothing.

## Output

Return concise structured output:

```markdown
## Risk-Adaptive Cross Review

**mode**: pr-review | openspec-review | hybrid-review
**risk**: low | medium | high
**reviewers**: <reviewer names>
**verdict**: approve | request-changes | needs-discussion

### Findings
- **P0/P1/P2** `<failure-class>`: per finding, all ten finding-contract fields —
  severity, failure class, violated invariant/contract, concrete scenario,
  evidence, consequence, fix direction, required test/proof, sibling surfaces,
  blocking status

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
- Use `subagent-workflow` for implementing a GitHub issue end to end.
