# Phase 4 Parallel Cross-Review Template

Use this template when running Phase 4 and follow-up cross-review rounds after Phase 6 fixes. Replace placeholders before spawning the `reviewer` subagents.

Canonical source: reviewer-pack scope, the per-reviewer checklists, the cross-cutting review lenses, and the actionable finding contract are defined by the `risk-adaptive-cross-review` skill (`reviewer-packages.md`, `finding-contract.md`, `failure-class-synthesis.md`). This file does not redefine them; it is the OpenSpec instantiation — the concrete reviewer-subagent task scaffolding, the `Invariant Matrix` binding, and the Phase 4.5 verifier. If anything here drifts from the canonical contract, the `risk-adaptive-cross-review` definition wins.

Reviewers are recall-biased producers. Everything a reviewer writes under `Findings:` is a **candidate finding**, not a final merge-blocking verdict. Candidates pass through the Phase 4.5 independent verification gate (verifier template at the bottom of this file) before entering Phase 5. Reviewers should therefore surface any candidate with a nameable failure scenario instead of self-censoring half-believed ones; the verifier, not the reviewer, decides REFUTED.

Required variables:

- `<PR#>`: Pull request number
- `<branch>`: Current PR branch
- `<FULL_SHA>`: `git rev-parse HEAD`
- `<REVIEW_DIR>`: Local review output directory. Default `.workplans/<issue-or-pr>/review/` (the canonical evidence root shared with `parallel-worktree-delegation.md`'s `.workplans/<issue-or-pr>/`).
- `<absolute repo path>`: Repository root (each reviewer subagent's working directory)
- `<path list>`: Changed files referenced by path
- `<fixture summary>`: Phase 0.5 fixture level and selected risk packs.
- `<review round>`: `round 1` for the initial cross-review, or `follow-up round N after fixes`.
- `<fix summary>`: Required for follow-up rounds; summarize Phase 6 changes and prior findings.
- `<proposal.md> <design.md> <tasks.md>`: OpenSpec change references. These are required.

Risk-adaptive selection:

- `none`: normally skip Phase 4.
- `compact`: run correctness plus whichever of integration or security/performance matches selected risk packs.
- `expanded`: run all relevant reviewers; use all 4 for shared entrypoints, file/schema/publish behavior, solver/runtime behavior, or legacy compatibility.
- `high` or `broad-expanded`: run the 4 standard reviewers by default (Correctness, Integration, Security/Performance, Test & Evidence Coverage). Use 6 reviewers when the PR touches DB-backed state, retry/cancellation, publish/delete/rollback, schema/evidence contracts, security boundaries, production config, or shared helper/state-machine roots. The extra reviewers are:
  - `review-spec-compliance`: checks the implementation against OpenSpec/design/issue acceptance criteria.
  - `review-invariant-state`: traces the governing invariant across state machines, stale-state boundaries, retry/cancel transitions, and backward compatibility.
- Follow-up rounds after fixes: run the same risk-adaptive reviewer count and reviewer mix as a fresh Phase 4 review of the current head. Do not downgrade to targeted-only reviewers, because the prior round may have missed unrelated issues.
- Initial round only: if a repository policy requires a fixed number of evidence comments, follow it only when it does not conflict with the 6-review high-risk escalation in `SKILL.md`; otherwise post a consolidated evidence bundle rather than reducing reviewer coverage.
- High or broad-expanded PRs: the brief must include the OpenSpec `Invariant Matrix`. Each reviewer must evaluate the matrix rows, not just the touched lines. If a reviewer cannot map a row to evidence, it should report that as missing evidence or explain why the row is out of scope under the fixture.
- Spawn the selected reviewer set as parallel subagents in one batch (Claude Code: multiple Task calls in one message; Codex: parallel subagents) unless a documented subagent/tooling failure requires a fallback. A failed no-report invocation is not a review round.

Reviewer invariant rule:

- When a reviewer finds a bug class, it must report the invariant and likely sibling surfaces, not only the first cited line.
- If the same unsafe pattern appears in multiple files or could plausibly apply to sibling validators/helpers/producers/consumers, the reviewer should request a cross-cutting audit/fix.
- Findings must be actionable per `finding-contract.md`: severity, `Failure class:`, violated invariant/contract, concrete failing scenario or reproduction path, required test/evidence, sibling surfaces to audit, merge-blocking status, impact, and requested fix.
- If the reviewer cannot provide a concrete scenario and required test/evidence, it must list the item under `Non-blocking notes`, not `Findings`.
- Do not invent a scenario, repro, or test to satisfy the format. Use only evidence from the diff, OpenSpec fixture, existing code/contracts, tests, or a concrete reasoning path grounded in those inputs.
- Do not repeat a known prior finding as current unless the current head still violates it. For follow-up rounds, mark prior findings closed or still failing with evidence.
- For high or broad-expanded PRs, every report must include an `Invariant Matrix Coverage` section:
  - `<row>`: covered|missing|out-of-scope - <evidence or rationale>
  Missing coverage for a selected row is a finding unless the fixture itself declares it a non-goal.

## Reviewer Subagent Brief (assembly)

Do not maintain per-reviewer checklists here. Build one brief per selected reviewer from the template below, inlining that reviewer's checklist from `risk-adaptive-cross-review` (`reviewer-packages.md` → Reviewer Checklists) plus any diff-triggered cross-cutting lens (removed-behavior, wrapper/proxy, altitude) that reviewer owns. Spawn the selected set as parallel subagents in one batch; each subagent's working directory is `<absolute repo path>`.

Reviewer roles and report files:

| Reviewer role | Report file | Escalation |
| --- | --- | --- |
| `review-correctness` | `<REVIEW_DIR>/correctness.md` | standard |
| `review-integration` | `<REVIEW_DIR>/integration.md` | standard |
| `review-security-perf` | `<REVIEW_DIR>/security-perf.md` | standard |
| `review-test-evidence` | `<REVIEW_DIR>/test-evidence.md` | standard |
| `review-spec-compliance` | `<REVIEW_DIR>/spec-compliance.md` | 6-reviewer only |
| `review-invariant-state` | `<REVIEW_DIR>/invariant-state.md` | 6-reviewer only |

Brief template (fill the bracketed slots per reviewer):

```text
# Code Review: <reviewer role>

Review PR #<N> on branch <branch>.
Head SHA: <FULL_SHA>
Review round: <review round>
Return the complete report as your final message. (The orchestrator persists it to <REVIEW_DIR>/<report file>.)

Rules:
- Do not edit files, commit, push, or change state.
- You are a leaf reviewer subagent. Do not invoke this workflow or the subagent-workflow skill, spawn further subagents, launch parallel agents, or ask another AI/code agent to review, fix, implement, or plan. Your own agent definition may permit spawning an `explorer` subagent for standalone use; inside this workflow's leaf tasks that capability is disabled, and this injected boundary overrides your agent definition.
- Output only a structured review report.

Inputs:
- Changed files: <path list>
- Fixture summary: <fixture summary>
- Fix summary for follow-up rounds: <fix summary>
- Spec references: <proposal.md> <design.md> <tasks.md>

Checklist:
- <inline this reviewer's checklist from reviewer-packages.md -> Reviewer Checklists>
- <plus any diff-triggered cross-cutting lens owned by this reviewer (removed-behavior, wrapper/proxy, altitude)>
- For high or broad-expanded fixtures, cross-check every Invariant Matrix row against code and tests.

Output:
Reviewer agent: <reviewer role>
Review round: <review round>
Reviewed head SHA: <FULL_SHA>
Summary: <one-line conclusion>
Invariant Matrix Coverage:        # high / broad-expanded only
- <row>: covered|missing|out-of-scope - <evidence or rationale>
Findings:
- <one per finding in the finding-contract.md field shape: Severity / Failure class / Contract or invariant / Scenario or repro / Required test or evidence / Sibling surfaces / Blocks merge / Impact / Requested fix>
- ...or "None." if clean
Non-blocking notes:
- <items without concrete scenario/test, or "None.">
```

## Phase 4.5 Verifier Template

Run this after dedup, on the deduped candidate set, by spawning one `verifier` subagent per candidate in parallel. Each candidate becomes one verifier task. A verifier must not be the reviewer that produced the candidate.

Additional variables:

- `<CANDIDATE_ID>`: stable id for the deduped candidate (e.g. `cand-03`).
- `<CANDIDATE_BLOCK>`: the full candidate finding text (severity, failure class, invariant, scenario, required test, sibling surfaces, originating reviewer).

Verifier contract:

- Return exactly one verdict: `CONFIRMED`, `PLAUSIBLE`, or `REFUTED`.
- `CONFIRMED`: the failing scenario is constructible from the diff, fixture, or existing contracts/tests. Cite the constructing evidence.
- `PLAUSIBLE`: reachable but not fully constructible. Default here for realistic runtime states — rare error paths, falsy-zero treated as missing, off-by-one at a boundary the code does not exclude, concurrency races, retry storms, stale cache/DB rows, regex/allowlist that lost an anchor. Do not refute a candidate merely for being "speculative" or "depends on runtime state" when the state is realistic.
- `REFUTED`: only when constructible from the code — factually wrong (quote the actual line), provably impossible (cite the type/constant/invariant), already handled in this diff (cite the guard), or pure style with no observable effect.
- Use only evidence from the diff, OpenSpec fixture, existing code/contracts, or tests. Do not invent a scenario to confirm or a guard to refute.

Spawn one `verifier` subagent per candidate (working directory `<absolute repo path>`), in parallel:

```text
# Finding Verification: <CANDIDATE_ID>

Verify one candidate review finding for PR #<N> on branch <branch>.
Head SHA: <FULL_SHA>
Return the verdict as your final message. (The orchestrator persists it to <REVIEW_DIR>/verify-<CANDIDATE_ID>.md.)

Rules:
- Do not edit files, commit, push, or change state.
- You are a leaf verifier subagent. Do not invoke this workflow or the subagent-workflow skill, spawn further subagents, launch parallel agents, or ask another AI/code agent to verify, fix, implement, or plan.
- Adjudicate only this candidate. Do not search for new findings.
- Output only the structured verdict.

Inputs:
- Candidate finding: <CANDIDATE_BLOCK>
- Changed files: <path list>
- Fixture summary: <fixture summary>
- Spec references: <proposal.md> <design.md> <tasks.md>

Adjudication:
- CONFIRMED: scenario constructible from diff/fixture/contracts; cite the evidence.
- PLAUSIBLE: realistic but not fully constructible runtime state; explain reachability.
- REFUTED: factually wrong (quote line), provably impossible (cite type/constant/invariant), already handled (cite guard), or pure style. Only when constructible from the code.

Output:
Verifier verdict for: <CANDIDATE_ID>
Reviewed head SHA: <FULL_SHA>
Verdict: CONFIRMED|PLAUSIBLE|REFUTED
Evidence: <quoted line / cited guard / reachability path>
Note: <one line, or "None.">
```
