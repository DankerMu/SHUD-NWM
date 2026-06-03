# Phase 4 Parallel Cross-Review Template

Use this template when running Phase 4 and follow-up cross-review rounds after Phase 6 fixes. Replace placeholders before invoking `codeagent-wrapper`.

Canonical source: reviewer-pack scope and the actionable finding contract are defined by the `risk-adaptive-cross-review` skill (`reviewer-packages.md`, `finding-contract.md`, `failure-class-synthesis.md`). This file does not redefine them; it is the codex/OpenSpec instantiation — the concrete `codeagent-wrapper` task scaffolding, the `Invariant Matrix` binding, and the Phase 4.5 verifier. If a checklist here drifts from the canonical contract, the `risk-adaptive-cross-review` definition wins.

Reviewers are recall-biased producers. Everything a reviewer writes under `Findings:` is a **candidate finding**, not a final merge-blocking verdict. Candidates pass through the Phase 4.5 independent verification gate (verifier template at the bottom of this file) before entering Phase 5. Reviewers should therefore surface any candidate with a nameable failure scenario instead of self-censoring half-believed ones; the verifier, not the reviewer, decides REFUTED.

Required variables:

- `<PR#>`: Pull request number
- `<branch>`: Current PR branch
- `<FULL_SHA>`: `git rev-parse HEAD`
- `<REVIEW_DIR>`: Local review output directory
- `<absolute repo path>`: Repository root
- `<@path list>`: Changed files referenced with `@`
- `<fixture summary>`: Phase 0.5 fixture level and selected risk packs.
- `<review round>`: `round 1` for the initial cross-review, or `follow-up round N after fixes`.
- `<fix summary>`: Required for follow-up rounds; summarize Phase 6 changes and prior findings.
- `<@proposal.md> <@design.md> <@tasks.md>`: OpenSpec change references. These are required.

Risk-adaptive selection:

- `none`: normally skip Phase 4.
- `compact`: run correctness plus whichever of integration or security/performance matches selected risk packs.
- `expanded`: run all relevant reviewers; use all 4 for shared entrypoints, file/schema/publish behavior, solver/runtime behavior, or legacy compatibility.
- `high` or `broad-expanded`: run the 4 standard reviewers by default. Use 6 reviewers when the PR touches DB-backed state, retry/cancellation, publish/delete/rollback, schema/evidence contracts, security boundaries, production config, or shared helper/state-machine roots. The extra reviewers are:
  - `review-test-evidence`: verifies task/regression coverage, local verification evidence, and unchanged consumer tests.
  - `review-invariant-state`: traces the governing invariant across state machines, stale-state boundaries, retry/cancel transitions, and backward compatibility.
- Follow-up rounds after fixes: run the same risk-adaptive reviewer count and reviewer mix as a fresh Phase 4 review of the current head. Do not downgrade to targeted-only reviewers, because the prior round may have missed unrelated issues.
- Initial round only: if a repository policy requires a fixed number of evidence comments, follow it only when it does not conflict with the 6-review high-risk escalation in `SKILL.md`; otherwise post a consolidated evidence bundle rather than reducing reviewer coverage.
- High or broad-expanded PRs: the prompt must include the OpenSpec `Invariant Matrix`. Each reviewer must evaluate the matrix rows, not just the touched lines. If a reviewer cannot map a row to evidence, it should report that as missing evidence or explain why the row is out of scope under the fixture.
- Use one `codeagent-wrapper --parallel --backend codex` invocation for the selected reviewer set unless a documented wrapper/tooling failure requires a fallback. A failed no-report invocation is not a review round.

Reviewer invariant rule:

- When a reviewer finds a bug class, it must report the invariant and likely sibling surfaces, not only the first cited line.
- If the same unsafe pattern appears in multiple files or could plausibly apply to sibling validators/helpers/producers/consumers, the reviewer should request a cross-cutting audit/fix.
- Findings must be actionable. Each finding must include severity, `Failure class:`, violated invariant/contract, concrete failing scenario or reproduction path, required test/evidence, sibling surfaces to audit, merge-blocking status, impact, and requested fix.
- If the reviewer cannot provide a concrete scenario and required test/evidence, it must list the item under `Non-blocking notes`, not `Findings`.
- Do not invent a scenario, repro, or test to satisfy the format. Use only evidence from the diff, OpenSpec fixture, existing code/contracts, tests, or a concrete reasoning path grounded in those inputs.
- Do not repeat a known prior finding as current unless the current head still violates it. For follow-up rounds, mark prior findings closed or still failing with evidence.
- Failure class examples: path binding, bounded read, JSON complexity, lane lifecycle, schema/audit contract, retry semantics, resource limit, compatibility drift.
- For high or broad-expanded PRs, every report must include an `Invariant Matrix Coverage` section:
  - `<row>`: covered|missing|out-of-scope - <evidence or rationale>
  Missing coverage for a selected row is a finding unless the fixture itself declares it a non-goal.

```bash
"$CODEAGENT" --parallel --backend codex --full-output <<'EOF'
---TASK---
id: review-spec-compliance
backend: codex
workdir: <absolute repo path>
---CONTENT---
# Code Review: Spec Compliance

Review PR #<N> on branch <branch>.
Head SHA: <FULL_SHA>
Review round: <review round>
Write the complete report to <REVIEW_DIR>/spec-compliance.md.

Rules:
- Do not edit files, commit, push, or change state.
- You are a leaf reviewer. Do not invoke codeagent-wrapper, use the codeagent skill, use codex-codeagent-workflow, spawn subagents, launch parallel agents, or ask another AI/code agent to review, fix, implement, or plan.
- Output only a structured review report.

Inputs:
- Changed files: <@path list>
- Fixture summary: <fixture summary>
- Fix summary for follow-up rounds: <fix summary>
- Spec references: <@proposal.md> <@design.md> <@tasks.md>

Checklist:
- Cross-check every task in tasks.md against the diff; mark DONE or MISSING.
- For high or broad-expanded fixtures, cross-check every Invariant Matrix row against code and tests.
- Flag scope creep not covered by the issue or OpenSpec change.
- Verify acceptance criteria.
- Check test coverage against tasks.md.
- Check selected risk packs are addressed.
- If a selected risk pack has repeated or analogous surfaces, verify the evidence covers the invariant across those surfaces, not only one example.

Output:
Reviewer agent: Spec Compliance Reviewer
Review round: <review round>
Reviewed head SHA: <FULL_SHA>
Summary: <one-line conclusion>
Invariant Matrix Coverage:
- <row>: covered|missing|out-of-scope - <evidence or rationale>
Findings:
- <Severity: critical/major/minor> Failure class: <class>. Contract/invariant: <rule>. Scenario/repro: <concrete case>. Required test/evidence: <test or command>. Sibling surfaces to audit: <files/helpers or "none">. Blocks merge: yes|no. Impact: <impact>. Requested fix: <fix>
- ...or "None." if clean
Non-blocking notes:
- <items without concrete scenario/test, or "None.">

---TASK---
id: review-correctness
backend: codex
workdir: <absolute repo path>
---CONTENT---
# Code Review: Correctness

Review PR #<N> on branch <branch>.
Head SHA: <FULL_SHA>
Review round: <review round>
Write the complete report to <REVIEW_DIR>/correctness.md.

Rules:
- Do not edit files, commit, push, or change state.
- You are a leaf reviewer. Do not invoke codeagent-wrapper, use the codeagent skill, use codex-codeagent-workflow, spawn subagents, launch parallel agents, or ask another AI/code agent to review, fix, implement, or plan.
- Output only a structured review report.

Inputs:
- Changed files: <@path list>
- Fixture summary: <fixture summary>
- Fix summary for follow-up rounds: <fix summary>
- Spec references: <@proposal.md> <@design.md> <@tasks.md>

Checklist:
- Logic correctness within each changed file.
- For high or broad-expanded fixtures, verify the governing invariant cannot be violated by valid boundary, stale, mismatched, or unauthorized inputs named by the Invariant Matrix.
- Function signatures match the target API.
- Edge cases: null/empty inputs, boundary values, off-by-one.
- Type safety at call sites.
- Control flow: error branches, early returns, loop bounds.
- Correctness against selected risk packs from the OpenSpec fixture.
- If a cited bug reveals a reusable unsafe pattern, identify all sibling call sites/helpers that should satisfy the same invariant.

Output:
Reviewer agent: Correctness Reviewer
Review round: <review round>
Reviewed head SHA: <FULL_SHA>
Summary: <one-line conclusion>
Invariant Matrix Coverage:
- <row>: covered|missing|out-of-scope - <evidence or rationale>
Findings:
- <Severity: critical/major/minor> Failure class: <class>. Contract/invariant: <rule>. Scenario/repro: <concrete case>. Required test/evidence: <test or command>. Sibling surfaces to audit: <files/helpers or "none">. Blocks merge: yes|no. Impact: <impact>. Requested fix: <fix>
- ...or "None." if clean
Non-blocking notes:
- <items without concrete scenario/test, or "None.">

---TASK---
id: review-integration
backend: codex
workdir: <absolute repo path>
---CONTENT---
# Code Review: Integration and Cross-file Impact

Review PR #<N> on branch <branch>.
Head SHA: <FULL_SHA>
Review round: <review round>
Write the complete report to <REVIEW_DIR>/integration.md.

Rules:
- Do not edit files, commit, push, or change state.
- You are a leaf reviewer. Do not invoke codeagent-wrapper, use the codeagent skill, use codex-codeagent-workflow, spawn subagents, launch parallel agents, or ask another AI/code agent to review, fix, implement, or plan.
- Output only a structured review report.

Inputs:
- Changed files: <@path list>
- Fixture summary: <fixture summary>
- Fix summary for follow-up rounds: <fix summary>
- Spec references: <@proposal.md> <@design.md> <@tasks.md>

Checklist:
- Return value contracts match downstream expectations.
- Removed-behavior audit: for every line the diff deletes or replaces, name the invariant, guard, validation, or error path it enforced, then locate where the new code re-establishes it. If you cannot find the re-establishment, that is a candidate finding (dropped guard, narrowed validation, removed error branch, deleted covering test).
- For high or broad-expanded fixtures, trace the source-of-truth identity/contract through every producer, validator, storage/cache/query, route/entrypoint, downstream consumer, failure path, and evidence surface named by the Invariant Matrix.
- Shared variables flow correctly from setup/config to consumers.
- Execution order satisfies prerequisites before first use.
- Unchanged consumers of changed outputs still work.
- Global state is explicit or intentionally preserved.
- Compatibility axes named in the OpenSpec fixture are preserved.
- For producer/consumer, receipt/summary, schema, and evidence flows, verify that all linked artifacts are bound to the same identity/contract and cannot be mixed from sibling paths or stale states.

Output:
Reviewer agent: Integration Reviewer
Review round: <review round>
Reviewed head SHA: <FULL_SHA>
Summary: <one-line conclusion>
Invariant Matrix Coverage:
- <row>: covered|missing|out-of-scope - <evidence or rationale>
Findings:
- <Severity: critical/major/minor> Failure class: <class>. Contract/invariant: <rule>. Scenario/repro: <concrete case>. Required test/evidence: <test or command>. Sibling surfaces to audit: <files/helpers or "none">. Blocks merge: yes|no. Impact: <impact>. Requested fix: <fix>
- ...or "None." if clean
Non-blocking notes:
- <items without concrete scenario/test, or "None.">

---TASK---
id: review-security-perf
backend: codex
workdir: <absolute repo path>
---CONTENT---
# Code Review: Security and Performance

Review PR #<N> on branch <branch>.
Head SHA: <FULL_SHA>
Review round: <review round>
Write the complete report to <REVIEW_DIR>/security-perf.md.

Rules:
- Do not edit files, commit, push, or change state.
- You are a leaf reviewer. Do not invoke codeagent-wrapper, use the codeagent skill, use codex-codeagent-workflow, spawn subagents, launch parallel agents, or ask another AI/code agent to review, fix, implement, or plan.
- Output only a structured review report.

Inputs:
- Changed files: <@path list>
- Fixture summary: <fixture summary>
- Fix summary for follow-up rounds: <fix summary>
- Spec references: <@proposal.md> <@design.md> <@tasks.md>

Checklist:
- Path safety: traversal, symlinks, overwrite behavior.
- For high or broad-expanded fixtures, verify adversarial/boundary rows in the Invariant Matrix have stable failures and cannot bypass the selected risk-pack controls.
- Resource management: file handles, connections, device open/close pairing.
- Unbounded operations: loops without guards, large allocations, memory safety.
- Data integrity: no silent numerical or semantic changes.
- Info leakage: tokens, keys, credentials in logs or output.
- Security/performance risk packs selected in the OpenSpec fixture are addressed.
- For path/evidence/resource findings, check analogous sibling modules and shared helper patterns before concluding the risk pack is closed.

Output:
Reviewer agent: Security and Performance Reviewer
Review round: <review round>
Reviewed head SHA: <FULL_SHA>
Summary: <one-line conclusion>
Invariant Matrix Coverage:
- <row>: covered|missing|out-of-scope - <evidence or rationale>
Findings:
- <Severity: critical/major/minor> Failure class: <class>. Contract/invariant: <rule>. Scenario/repro: <concrete case>. Required test/evidence: <test or command>. Sibling surfaces to audit: <files/helpers or "none">. Blocks merge: yes|no. Impact: <impact>. Requested fix: <fix>
- ...or "None." if clean
Non-blocking notes:
- <items without concrete scenario/test, or "None.">

---TASK---
id: review-test-evidence
backend: codex
workdir: <absolute repo path>
---CONTENT---
# Code Review: Test and Evidence Coverage

Review PR #<N> on branch <branch>.
Head SHA: <FULL_SHA>
Review round: <review round>
Write the complete report to <REVIEW_DIR>/test-evidence.md.

Rules:
- Do not edit files, commit, push, or change state.
- You are a leaf reviewer. Do not invoke codeagent-wrapper, use the codeagent skill, use codex-codeagent-workflow, spawn subagents, launch parallel agents, or ask another AI/code agent to review, fix, implement, or plan.
- Output only a structured review report.

Inputs:
- Changed files: <@path list>
- Fixture summary: <fixture summary>
- Fix summary for follow-up rounds: <fix summary>
- Spec references: <@proposal.md> <@design.md> <@tasks.md>

Checklist:
- Map every selected OpenSpec task and required evidence row to an explicit test, command, fixture, or justified non-goal.
- For high or broad-expanded fixtures, verify every Invariant Matrix row has both positive and failure/boundary regression evidence where applicable.
- Verify tests exercise real integration surfaces when the fixture requires real DB/state/orchestrator/API behavior; fake-only coverage is insufficient unless the fixture says so.
- Check unchanged downstream consumer compatibility tests named by the fixture.
- Check local verification commands cover touched modules and do not omit a selected risk pack.
- Identify stale, overbroad, or misleading evidence claims.

Output:
Reviewer agent: Test and Evidence Coverage Reviewer
Review round: <review round>
Reviewed head SHA: <FULL_SHA>
Summary: <one-line conclusion>
Invariant Matrix Coverage:
- <row>: covered|missing|out-of-scope - <evidence or rationale>
Findings:
- <Severity: critical/major/minor> Failure class: <class>. Contract/invariant: <rule>. Scenario/repro: <concrete case>. Required test/evidence: <test or command>. Sibling surfaces to audit: <files/helpers or "none">. Blocks merge: yes|no. Impact: <impact>. Requested fix: <fix>
- ...or "None." if clean
Non-blocking notes:
- <items without concrete scenario/test, or "None.">

---TASK---
id: review-invariant-state
backend: codex
workdir: <absolute repo path>
---CONTENT---
# Code Review: Invariant, State Machine, and Compatibility

Review PR #<N> on branch <branch>.
Head SHA: <FULL_SHA>
Review round: <review round>
Write the complete report to <REVIEW_DIR>/invariant-state.md.

Rules:
- Do not edit files, commit, push, or change state.
- You are a leaf reviewer. Do not invoke codeagent-wrapper, use the codeagent skill, use codex-codeagent-workflow, spawn subagents, launch parallel agents, or ask another AI/code agent to review, fix, implement, or plan.
- Output only a structured review report.

Inputs:
- Changed files: <@path list>
- Fixture summary: <fixture summary>
- Fix summary for follow-up rounds: <fix summary>
- Spec references: <@proposal.md> <@design.md> <@tasks.md>

Checklist:
- Trace the governing invariant across producers, validators, storage/cache/query, public entrypoints, downstream consumers, failure paths, stale-state boundaries, and evidence surfaces.
- For state-machine work, verify transition ordering, terminal/active/permanent/manual states, retry limits, cancellation proof gaps, stale DB/cache boundaries, and no duplicate or lost transition.
- Verify candidate/run/object identity cannot collapse across siblings, stale rows, aggregate rows, or partial-success manifests.
- Verify backward compatibility for unchanged consumers and older persisted state shapes.
- If one issue exposes a reusable unsafe state/helper pattern, identify the full sibling surface that must be audited or fixed.

Output:
Reviewer agent: Invariant and State Compatibility Reviewer
Review round: <review round>
Reviewed head SHA: <FULL_SHA>
Summary: <one-line conclusion>
Invariant Matrix Coverage:
- <row>: covered|missing|out-of-scope - <evidence or rationale>
Findings:
- <Severity: critical/major/minor> Failure class: <class>. Contract/invariant: <rule>. Scenario/repro: <concrete case>. Required test/evidence: <test or command>. Sibling surfaces to audit: <files/helpers or "none">. Blocks merge: yes|no. Impact: <impact>. Requested fix: <fix>
- ...or "None." if clean
Non-blocking notes:
- <items without concrete scenario/test, or "None.">
EOF
```

## Phase 4.5 Verifier Template

Run this after dedup, on the deduped candidate set, as one parallel invocation. Each candidate becomes one verifier task. A verifier must not be the reviewer that produced the candidate.

Additional variables:

- `<CANDIDATE_ID>`: stable id for the deduped candidate (e.g. `cand-03`).
- `<CANDIDATE_BLOCK>`: the full candidate finding text (severity, failure class, invariant, scenario, required test, sibling surfaces, originating reviewer).

Verifier contract:

- Return exactly one verdict: `CONFIRMED`, `PLAUSIBLE`, or `REFUTED`.
- `CONFIRMED`: the failing scenario is constructible from the diff, fixture, or existing contracts/tests. Cite the constructing evidence.
- `PLAUSIBLE`: reachable but not fully constructible. Default here for realistic runtime states — rare error paths, falsy-zero treated as missing, off-by-one at a boundary the code does not exclude, concurrency races, retry storms, stale cache/DB rows, regex/allowlist that lost an anchor. Do not refute a candidate merely for being "speculative" or "depends on runtime state" when the state is realistic.
- `REFUTED`: only when constructible from the code — factually wrong (quote the actual line), provably impossible (cite the type/constant/invariant), already handled in this diff (cite the guard), or pure style with no observable effect.
- Use only evidence from the diff, OpenSpec fixture, existing code/contracts, or tests. Do not invent a scenario to confirm or a guard to refute.

```bash
"$CODEAGENT" --parallel --backend codex --full-output <<'EOF'
---TASK---
id: verify-<CANDIDATE_ID>
backend: codex
workdir: <absolute repo path>
---CONTENT---
# Finding Verification: <CANDIDATE_ID>

Verify one candidate review finding for PR #<N> on branch <branch>.
Head SHA: <FULL_SHA>
Write the verdict to <REVIEW_DIR>/verify-<CANDIDATE_ID>.md.

Rules:
- Do not edit files, commit, push, or change state.
- You are a leaf verifier. Do not invoke codeagent-wrapper, use the codeagent skill, use codex-codeagent-workflow, spawn subagents, launch parallel agents, or ask another AI/code agent to verify, fix, implement, or plan.
- Adjudicate only this candidate. Do not search for new findings.
- Output only the structured verdict.

Inputs:
- Candidate finding: <CANDIDATE_BLOCK>
- Changed files: <@path list>
- Fixture summary: <fixture summary>
- Spec references: <@proposal.md> <@design.md> <@tasks.md>

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
EOF
```
