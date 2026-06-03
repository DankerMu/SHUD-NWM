# Phase Flow Details

Load this reference when actively running the workflow. Keep issue-specific prompts concise and refer to the OpenSpec change fixture instead of restating it.

## Phase 0: Issue Selection + OpenSpec Change Discovery

Skip DAG selection if the user specified an issue number.

1. Run `gh issue list --state open --limit 50 --json number,title,labels,state`.
2. Read each non-epic issue body for `Depends on #XX`; cross-reference with closed issues.
3. Auto-select by phase `p0 > p1 > p2 > p3`, then priority `critical > high > medium`, then `type:backend` over `type:android`, then lower issue number.
4. Read the selected issue body and relevant comments. Extract an OpenSpec change name if the issue names one.
5. Locate `openspec/changes/<change-name>/{proposal.md,design.md,tasks.md}` in the active repo or workspace. If the change is missing, create it in Phase 0.5 before implementation.
6. Resolve the project profile (lookup order):
   1. If `openspec/project-profile.md` exists, load it as the active profile.
   2. If it is absent, run the one-time **profile bootstrap** (Phase 0.0 below), which writes `openspec/project-profile.md`, then load it.
   3. Only if bootstrap cannot infer anything project-specific, fall back to the **Generic** profile in `project-profiles.md`.
   Then load `issue-risk-contract.md`.
7. Announce the DAG sketch, the active project profile, and whether the OpenSpec change exists.

### Phase 0.0: Profile Bootstrap (one-time per project)

Run only when `openspec/project-profile.md` is missing. The shared
`project-profiles.md` provides the Generic default and SHUD/rSHUD/AutoSHUD
example templates to copy from; the active profile is project-local and survives
skill reinstalls because it lives under `openspec/`, not inside the skill.

1. Scan the repo for risk-bearing structure: primary language/build system, public entrypoints (CLI, API, services), data schemas/formats/serializers, external integrations and credentials, persisted/shared state, and shared helper roots.
2. Match against `project-profiles.md`. If an example profile fits (e.g. an AutoSHUD repo), copy it as the starting point; otherwise start from Generic.
3. Write `openspec/project-profile.md` with the six profile fields: entry surfaces, contracts, risk axes, typical evidence, domain risk packs, domain expanded-triggers. Respect the size budget in `project-profiles.md`: short bullets not prose, never restate core packs/triggers, and stay under ~25 lines for a simple project or ~60 for a broad multi-subsystem system. Over-budget means it restates core or the repo should split into narrower profiles.
4. Note in the file that it is a living artifact maintained in Phase 0.5 as the project evolves.

The profile is a living document, not a one-shot. It does not change per issue, but it is updated whenever the project grows a new risk surface (see Phase 0.5 profile-gap maintenance).

## Phase 0.5: Risk Triage + OpenSpec Fixture

1. Create a short triage from issue, repo context, expected change surface, and the active project profile (`openspec/project-profile.md`).
   - Profile-gap maintenance: if the issue touches an entry surface, contract, risk axis, or domain pack the profile does not yet describe, update `openspec/project-profile.md` before continuing. Keep the profile a living artifact; do not edit it for ordinary issues that already fit it.
2. Assign fixture level: `none`, `compact`, or `expanded`. Mandatory expanded triggers live in `issue-risk-contract.md`.
3. Assign repair intensity:
   - `low`: isolated, low blast-radius change; focused fixes are acceptable.
   - `medium`: normal implementation with selected risk packs; second same-class finding triggers pattern escalation.
   - `high`: file IO/path safety, auth/permissions, evidence chains, publish/delete/rollback, production config, security, money, data loss, or shared helper behavior; first critical/major reusable-pattern finding triggers invariant inventory.
   - `broad-expanded`: expanded fixture spanning shared helpers plus multiple business lanes; front-load shared helper/boundary contracts and use high-risk escalation rules.
4. Every risk pack must be marked `selected` or `not selected` with a short reason.
5. If missing, create the change:
   ```bash
   openspec new change <change-name> --description "<short issue summary>"
   openspec instructions --change <change-name>
   ```
6. Codex may directly edit `openspec/changes/<change>/**`; keep artifacts concise and focused.
7. Ensure `tasks.md` maps every selected risk pack to a scenario-level test, verification command, or explicit non-goal with input and expected output.
8. For high or broad-expanded repair intensity, add a compact `Invariant Matrix` to the fixture before implementation. This is a hard gate, not an optional review aid:
   ```text
   Invariant Matrix
   Governing invariant: <one sentence rule that must hold end to end>
   Source-of-truth identity/contract: <field, object, path, role, digest, version, or schema>
   Surfaces:
   - Producers: <files/functions or "none - reason">
   - Validators/preflight: <files/functions or "none - reason">
   - Storage/cache/query: <files/functions or "none - reason">
   - Public routes/entrypoints: <files/functions or "none - reason">
   - Frontend/downstream consumers: <files/functions or "none - reason">
   - Failure paths/rollback/stale state: <files/functions or "none - reason">
   - Evidence/audit/readiness: <files/functions/artifacts or "none - reason">
   Regression rows:
   - <surface + valid input/identity> -> <expected behavior>
   - <surface + mismatched/stale/unauthorized/boundary input> -> <expected stable failure>
   - <unchanged sibling consumer> -> <expected compatibility behavior>
   ```
9. For high or broad-expanded repair intensity, also add a compact boundary-surface checklist to the fixture. Use only relevant categories: shared helper roots, public entrypoints, read surfaces, write/delete/overwrite surfaces, staging/publish/rollback surfaces, producer/consumer evidence boundaries, stale-state/idempotency boundaries, and unchanged downstream consumers.
10. Run one focused fixture review via `codeagent-wrapper --backend codex`; reviewer is read-only and checks only fixture completeness. For high or broad-expanded fixtures, the review must specifically validate the `Invariant Matrix`; if it is absent or too vague, the fixture review is `revise`.
11. If review says `revise`, update the OpenSpec change and rerun the fixture review once.
12. Run `openspec validate <change-name> --strict --no-interactive`; do not proceed until it passes.

## Phase 1: codeagent Implementation

1. Resolve codeagent:
   ```bash
   CODEAGENT=$(which codeagent-wrapper 2>/dev/null || echo "$HOME/.claude/bin/codeagent-wrapper")
   test -x "$CODEAGENT"
   ```
2. Create branch from integration base:
   ```bash
   git checkout -b feat/issue-<N>-<change-name> master
   ```
3. Decide whether the implementation can be safely split into parallel code-writing slices. Use serial implementation for one shared state machine, one shared helper root, one schema/contract/public API boundary, uncertain design, or overlapping tests. If parallel implementation is safe, load and follow `parallel-worktree-delegation.md` before delegating any code-writing worker. The parent PR worktree remains the integration surface.
4. Prompt must include:
   - Required delegation guard from `SKILL.md`.
   - Issue reference, `@proposal.md`, `@design.md`, `@tasks.md`, fixture level/risk packs, and key source files.
   - Repair intensity and any boundary-surface checklist from Phase 0.5.
   - The `Invariant Matrix` for high or broad-expanded work, with an instruction that implementation must preserve the governing invariant across every listed surface and report unchanged sibling surfaces inspected.
   - Clear scope and acceptance criteria.
   - Explicit instruction that implementation and tests ship together.
   - Full `tasks.md` test/evidence section.
   - Project verification commands.
   - Instruction to list changed files and verification results.
   - For high or broad-expanded work, instruction to report shared helper/boundary surfaces inspected, which were changed, and which were intentionally left unchanged with rationale.
   - For high or broad-expanded work, instruction to report regression evidence for every `Invariant Matrix` row, or explain why a row is out of scope only by citing the fixture.
5. Invoke either a serial task or the parallel worktree delegation defined by `parallel-worktree-delegation.md`:
   ```bash
   "$CODEAGENT" --backend codex --full-output - "$(pwd)" <<'EOF'
   <implementation prompt>
   EOF
   ```
6. Use timeouts by complexity: simple 30 min, medium 1 h, complex 2 h. Do not kill a running codeagent task. While it runs, wait silently with a long tool timeout; do not poll every few seconds or stream intermediate logs unless diagnosing a stuck/failing task. If it fails, refine prompt and retry, up to 2 attempts.

## Phase 2: Codex Verification Only

Run the local CI-equivalent pipeline for the project. Examples:

- Node: `npm run build`, `npm test`
- Android: `cd packages/android && ./gradlew detekt ktlintMainSourceSetCheck ktlintTestSourceSetCheck ktlintAndroidTestSourceSetCheck testDebugUnitTest assembleDebug`
- R package: focused tests plus package check command appropriate to repo
- SHUD solver: build plus smallest relevant example/smoke run

Then do a read-only audit against the OpenSpec fixture:

- Error/failure paths have tests.
- Selected risk packs are evidenced.
- CI-sensitive timers/processes are stable.
- Existing consumers and compatibility axes still work.

If any issue is found, create a precise Phase 6 style fix prompt. Do not patch implementation files directly.

## Phase 3: Commit and PR

1. Review `git status` and `git diff`.
2. Stage specific files only; never use `git add -A`.
3. Commit with conventional format: `feat(<scope>): <desc> (#<issue>)`.
4. Push branch.
5. Create PR. Leave Agent Review section empty until Phase 8.

## Phase 4: Risk-Adaptive Cross-Review

Select reviewers from fixture level:

- `none`: skip unless Phase 2 audit finds risk.
- `compact`: run 1-2 reviewers focused on changed behavior and selected risk packs.
- `expanded`: run 2-4 reviewers; use all 4 for shared entrypoints, file/schema/publish behavior, solver/runtime behavior, or legacy compatibility.
- `high` or `broad-expanded`: use all 4 standard reviewers. Escalate to 6 reviewers when the PR touches DB-backed state, retry/cancellation, publish/delete/rollback, schema/evidence contracts, security boundaries, production config, or shared helper/state-machine roots. The two additional reviewers are `Test & Evidence Coverage` and `Invariant / State Machine / Compatibility`.
- Initial round only: if repository policy requires a fixed number of evidence comments, follow it only when it does not conflict with the 6-review high-risk escalation in `SKILL.md`; otherwise post a consolidated evidence bundle rather than reducing reviewer coverage.

Use `phase-4-cross-review.md` to build the parallel codeagent prompt. Prefer one `codeagent-wrapper --parallel --backend codex` invocation for the full reviewer set. Do not post PR comments in this phase.

Review rounds:

- Round 1 uses the risk-adaptive reviewer count above.
- After a Phase 6 fix pass, rerun cross-review before Phase 7 using the same risk-adaptive reviewer count and reviewer mix as Phase 4 on the current head.
- Do not narrow follow-up rounds to only the risk areas touched by the fix. A prior round can miss issues outside the fix area, so each post-fix round must be a comprehensive review of the updated PR diff and OpenSpec fixture before Phase 7.
- A cross-review round is clean only when it has no actionable findings. Critical/major findings and test coverage gaps always return to Phase 5-6. Minor findings must be fixed or explicitly deferred with issue/OpenSpec/user-instruction basis.
- A finding is actionable only if it satisfies the finding contract defined in the `risk-adaptive-cross-review` skill (`finding-contract.md`): severity, failure class, violated invariant/contract, concrete scenario, evidence, fix direction, required test/proof, sibling surfaces, and blocking status. Treat vague concerns, style preferences, and untestable possibilities as non-blocking notes unless Codex can complete the missing fields from the diff and fixture.
- If a follow-up round finds the same failure class in another module, helper, or sibling surface, treat that as an invariant miss, not as a new isolated finding. Trigger Phase 6.2 before issuing the next fix prompt.
- If Round 3 still reports the same failure class after an invariant-closure pass, stop ordinary review looping. Run a Review Failure Retro before another fix or review pass.
- If the PR reaches 5 comprehensive cross-review rounds total, the five-round hard gate triggers. Stop ordinary review/fix looping immediately and transition to a root-cause strategy path; this is not permission to abandon the issue. Do not run another codeagent fix, cross-review, Phase 7 final review, CI wait-for-merge, or merge until a Deep Review Failure Retro, Gate-Level PR Strategy Review, Invariant Surface Inventory, and Regression Matrix are written to the local evidence directory or PR working notes.
- If review/fix activity has consumed more than one working day, or the same invariant keeps failing in sibling surfaces, run a Review Failure Retro before any further review round. The retro must change the next action: update fixture/matrix, broaden or split implementation scope, strengthen reviewer prompts, or make a user-visible scope call only when the decision cannot be derived from issue/OpenSpec evidence.
- While reviewers run, use the same silent long-wait rule as Phase 1. Avoid verbose `tail`, `watch`, or frequent status polling unless a reviewer fails or exceeds the expected timeout.
- If parallel review tooling fails without producing reports, diagnose the wrapper/API failure once, then rerun the same reviewer set with `codeagent-wrapper --parallel`. Do not count a failed no-report invocation as a comprehensive review round.
- Phase 4 reviewers emit candidate findings, not final merge-blocking verdicts. Do not feed reviewer reports straight into Phase 5; they must pass the Phase 4.5 verification gate first.

## Phase 4.5: Independent Finding Verification Gate

Reviewers are recall-biased producers; this gate separates finding from judging so false positives never spawn fix rounds or inflate the round budget.

Steps:

1. Collect every candidate finding from the Phase 4 reports actually run on the current head.
2. Dedup near-duplicates: same defect + same location + same root reason collapse to one candidate. Merge the cited sibling surfaces and keep the highest severity.
3. Run one `codeagent-wrapper --parallel --backend codex` verification pass using the verifier template in `phase-4-cross-review.md`. Assign each candidate to a separate verifier task. A verifier must not be the reviewer that produced the candidate, and Codex must not self-adjudicate in place of a verifier.
4. Each verifier returns exactly one verdict:
   - `CONFIRMED`: the failing scenario is constructible from the diff/fixture/contracts.
   - `PLAUSIBLE`: reachable but not fully constructible — rare error paths, falsy-zero treated as missing, off-by-one at a non-excluded boundary, races, retry storms, stale cache/DB rows, regex/allowlist that lost an anchor.
   - `REFUTED`: factually wrong (quote the line), provably impossible (cite type/constant/invariant), already handled in this diff (cite the guard), or pure style with no observable effect.
5. Apply the fixture-level bias:
   - `high` / `broad-expanded`: CONFIRMED and PLAUSIBLE are both merge-blocking inputs to Phase 5 (recall-biased).
   - `expanded`: CONFIRMED is merge-blocking; PLAUSIBLE is merge-blocking only when it maps to a selected risk pack or Invariant Matrix row, otherwise a non-blocking note.
   - `low` / `compact`: only CONFIRMED is merge-blocking; PLAUSIBLE becomes a non-blocking note (precision-biased).
6. Drop every REFUTED candidate and each `wontfix` test-coverage exception is still forbidden. Record one line per dropped candidate: `<candidate> -> REFUTED: <verifier rationale>`. Persist the verdict table to the local review directory.

Rules:

- This verification pass is not a comprehensive cross-review round. Do not increment the Phase 4 round counter for it.
- Never upgrade a REFUTED candidate back to blocking without a fresh reviewer finding on a later head.
- If verification tooling fails without producing verdicts, rerun it once; do not silently promote all candidates to blocking, and do not silently drop them.
- Verdicts must use only evidence from the diff, OpenSpec fixture, existing code/contracts, or tests. A verifier may not invent a scenario to confirm or a guard to refute.

## Phase 5: Fix Synthesis

Combine:

- OpenSpec fixture and selected risk packs
- Verified findings from Phase 4.5 (CONFIRMED plus risk-weighted PLAUSIBLE), including follow-up rounds after fixes
- Codex read-only diff review
- Local verification failures
- Test coverage gaps from `tasks.md`

First classify all findings by failure class and selected risk pack. Common classes include:

- path binding / traversal / symlink / overwrite
- evidence ingestion / bounded reads / JSON complexity
- schema / audit / receipt contract
- lane lifecycle / existing artifact state / partial outputs
- public CLI / config / environment boundary
- publish / rollback / cleanup semantics
- numerical / resource / runtime bounds
- compatibility / legacy consumer drift

Drop or downgrade non-actionable review notes before fix planning unless Codex can complete the actionable finding contract from the diff, fixture, and verification evidence.

Phase 4.5 already adjudicated each candidate. Do not re-litigate REFUTED candidates here. Treat the CONFIRMED and risk-weighted PLAUSIBLE set as the actionable input. The only additional filter at this stage is the `wontfix` rule below for findings the OpenSpec fixture, issue text, or explicit user instruction places out of scope; test coverage gaps are never `wontfix`.

Produce one checklist grouped by failure class. For each group include severity, file/line examples when available, violated invariant, concrete failing scenario, requested behavior, required test/evidence, selected risk pack, analogous surfaces that must be audited, and merge-blocking status. A group may cite multiple reviewer comments, but it should produce one class-level fix prompt.

Rules:

- `wontfix` must cite spec/design, issue text, OpenSpec non-goal, or explicit user instruction.
- Test coverage gaps are never `wontfix`.
- Do not produce one Phase 6 prompt per cited line when several findings share the same failure class. Merge them into one invariant or class-level closure task.
- Determine whether a hard-gate escalation applies before writing a fix prompt:
  - `low`: no hard escalation unless a critical reusable pattern appears.
  - `medium`: escalate on the second same-class finding.
  - `high`: escalate on the first critical/major finding that plausibly applies beyond the cited line.
  - `broad-expanded`: escalate on the first critical/major finding in a shared helper, file/evidence IO, auth, publish/delete/rollback, production config, or producer/consumer boundary.
- If a hard-gate escalation triggers, stop ordinary fixes. Produce an `Invariant Surface Inventory` first. Do not enter Phase 6 until it exists.
- If the same failure class appears in two review rounds, or if one finding exposes a reusable unsafe helper/pattern, create a Pattern Escalation section before Phase 6:
  ```text
  Pattern escalation: yes|no
  Failure class: <name>
  Invariant: <one sentence safety/correctness rule>
  Trigger: <rounds/findings that repeated or exposed shared pattern>
  Invariant Surface Inventory:
  - Shared helper roots: <helpers or "none">
  - Public entrypoints: <entrypoints or "none">
  - Read surfaces: <files/functions or "none">
  - Write/delete/overwrite surfaces: <files/functions or "none">
  - Staging/publish/rollback surfaces: <files/functions or "none">
  - Producer/consumer evidence boundaries: <files/functions/artifacts or "none">
  - Stale-state/idempotency boundaries: <files/functions/artifacts or "none">
  - Unchanged downstream consumers: <consumers or "none">
  Surfaces intentionally out of scope:
  - <surface> because <spec/user/non-goal reason>
  Regression matrix:
  - <surface + failure input> -> <expected stable behavior>
  - <surface + boundary input> -> <expected stable behavior>
  ```
- When pattern escalation is `yes`, the next fix prompt must be cross-cutting. Do not create one prompt per cited line.
- For high/broad-expanded escalations, the regression matrix must include at least one negative/adversarial case for each affected surface category, unless the category is explicitly `none` or out of scope.
- If cross-review reports are clean and coverage complete, and no ordinary-loop gate has triggered, skip Phase 6 and continue toward Phase 7.
- If the same failure class survives into a third comprehensive review round after a Phase 6.2/invariant-closure pass, write a Review Failure Retro instead of another ordinary fix list:
  ```text
  Review Failure Retro:
  Failure class: <name>
  Rounds affected: <rounds>
  Why Phase 5/6 did not close it:
  - Fixture scope gap: yes|no - <reason>
  - Fix prompt too narrow: yes|no - <reason>
  - Reviewer finding contract vague/inconsistent: yes|no - <reason>
  - Missing regression evidence: yes|no - <reason>
  - PR too broad / should split: yes|no - <reason>
  Next corrective action:
  - <invariant closure retry | fixture update | PR split | user scope decision | reviewer downgrade with rationale>
  ```
- If the PR has reached 5 comprehensive cross-review rounds total, write the stricter gate package instead of another ordinary fix list:
  ```text
  Deep Review Failure Retro:
  PR: #<N>
  Current head SHA: <sha>
  Comprehensive review rounds counted: <1..5>
  Round SHAs/reports:
  - Round <n>: <sha> <report paths/comments> <clean|findings summary>
  Repeated or moving failure classes:
  - <failure class>: <rounds and sibling surfaces>
  Why prior fixes did not close the invariant:
  - Fixture scope gap: yes|no - <reason>
  - Fix prompt too narrow: yes|no - <reason>
  - Reviewer contract vague/inconsistent: yes|no - <reason>
  - Missing regression evidence: yes|no - <reason>
  - PR too broad / should split: yes|no - <reason>

  Gate-Level PR Strategy Review:
  Direction check:
  - Is the PR solving the right problem from the issue/OpenSpec, or has it drifted? <answer with evidence>
  Architecture/refactor check:
  - Is the current code shape fighting the requirement? Does the fix need a refactor, new shared abstraction, or rollback/reimplementation instead of more patches? <answer>
  Loop check:
  - Are findings moving between sibling surfaces because the workflow is chasing symptoms? <answer>
  Functionality root-cause check:
  - Does the implementation fundamentally satisfy the user-visible feature contract, including failure paths and compatibility? <answer>
  Security/safety root-cause check:
  - Does the implementation fundamentally close selected security/safety invariants such as path/auth/evidence/publish/delete/data-loss boundaries? <answer>
  Decision:
  - <continue with invariant closure | refactor/redesign | split PR within issue/OpenSpec boundary | revise OpenSpec scope | ask user for scope/product decision only if not derivable from issue/OpenSpec | downgrade non-actionable reviewer pattern>
  Execution plan:
  - <concrete next codeagent/spec task, verification command, and expected evidence>

  Invariant Surface Inventory:
  - Shared helper roots: <helpers or "none">
  - Public entrypoints: <entrypoints or "none">
  - Read surfaces: <files/functions or "none">
  - Write/delete/overwrite surfaces: <files/functions or "none">
  - Staging/publish/rollback surfaces: <files/functions or "none">
  - Producer/consumer evidence boundaries: <files/functions/artifacts or "none">
  - Stale-state/idempotency boundaries: <files/functions/artifacts or "none">
  - Unchanged downstream consumers: <consumers or "none">

  Regression Matrix:
  - <surface + normal input> -> <expected behavior>
  - <surface + boundary/failure/security input> -> <expected stable behavior>
  - <unchanged sibling/downstream consumer> -> <expected compatibility behavior>

  Post-gate budget:
  - After the chosen corrective action, run at most one comprehensive cross-review.
  - If that review reports any critical/major finding in the same invariant family, do not return to narrow line-item repair. Re-enter this strategy review, update the gate package, and choose a stronger root-cause action such as redesign/refactor, fixture revision, PR split, or reviewer-pattern downgrade.
  - Escalate to the user only when the stronger action requires a product/scope decision that cannot be resolved from the issue and OpenSpec fixture.
  ```
- The gate package must be persisted before any further implementation/review action. It is not enough to summarize it in chat. After it is persisted, continue the workflow by executing the selected root-cause action unless a genuine product/scope decision is required from the user.
- Do not edit implementation files while synthesizing.

## Phase 6: codeagent Fix Pass

Before delegating fixes, decide whether parallel writing is safe. If any Phase 6 code-writing fix is parallelized, load and follow `parallel-worktree-delegation.md`. Use serial fixing for one shared state machine, one shared helper root, one schema/contract/public API boundary, uncertain design, or overlapping tests. Read-only audits may still run in parallel without worktrees.

Serial fix prompt template:

```text
<Required delegation guard>

# Fix List for PR #<N>

## Fix 1: <title> (<severity>)
File: <exact path>
Problem: <what is wrong>
Fix: <exact change to make>
Test: <new or changed test case>

## Verification
After all changes: <build+lint+test command>
All must pass. Do not break existing tests.
List changed files and verification results.
```

For pattern escalation or high-risk classes, replace the narrow fix list with an invariant-closure prompt:

```text
<Required delegation guard>

# Invariant Closure for PR #<N>

Failure class: <path binding|evidence ingestion|...>
Selected risk packs: <packs>
Invariant:
- <precise rule that must hold everywhere>

Do not only patch the cited line. Audit all sibling validators/helpers/callers/producers/consumers for this invariant.

Required audit surface:
- Shared helper roots: <helpers or "none">
- Public entrypoints: <entrypoints or "none">
- Read surfaces: <files/functions or "none">
- Write/delete/overwrite surfaces: <files/functions or "none">
- Staging/publish/rollback surfaces: <files/functions or "none">
- Producer/consumer evidence boundaries: <files/functions/artifacts or "none">
- Stale-state/idempotency boundaries: <files/functions/artifacts or "none">
- Unchanged downstream consumers: <consumers or "none">

Required behavior:
- <cross-cutting behavior>
- <stable error/compatibility contract>

Required regression matrix:
- <surface + failure input> -> <expected stable output>
- <surface + boundary input> -> <expected stable output>

Report:
- Files/functions inspected
- Unsafe matching patterns found
- Files changed
- Files intentionally not changed with rationale
- Surface inventory coverage: for every required audit surface, state clean|changed|out-of-scope and why
- Verification commands/results
```

Run Phase 2 verification after codeagent returns. If verification passes, commit/push the fix and rerun a full Phase 4-style cross-review on the current head, except for CI-only repairs classified in Phase 8. Continue Phase 5-6-review only while no ordinary-loop gate has triggered and until the latest comprehensive cross-review round is clean.

Before cross-review after a pattern escalation, run Phase 6.2. Do not skip Phase 6.2 merely because local tests passed.

Before committing or finishing any Phase 6 fix round that used delegated worktrees, complete the cleanup checklist in `parallel-worktree-delegation.md` and persist any intentionally retained worktree path and reason in the evidence directory.

Continue fix/review loops only while no ordinary-loop gate has triggered. Count repeated same-class findings as invariant misses requiring another cross-cutting closure pass, not as fresh isolated issues. Do not continue ordinary loops past a third same-class round; use Review Failure Retro to change the plan. Do not continue ordinary loops at or after 5 comprehensive cross-review rounds; use the five-round gate package to decide whether the PR direction, architecture, implementation strategy, feature contract, or security/safety invariant is wrong, then continue by executing the selected root-cause corrective action. Escalate only for real blockers, contradictory requirements, missing tooling, or unresolved product/scope decisions.

## Phase 6.2: Invariant Audit

Run this phase when Phase 5 marks pattern escalation or when a high-risk selected pack has a reusable unsafe pattern.

Codex may do the audit read-only, or delegate one read-only codeagent audit. The audit must not edit files. It must answer:

```text
Invariant audit for PR #<N>
Failure class: <name>
Invariant: <one sentence>
Selected risk packs: <packs>
Changed files and sibling surfaces: <@files>

Check:
- Find all matching code patterns, not just changed lines.
- Verify every sibling surface either satisfies the invariant or is explicitly out of scope.
- Verify unchanged analogous surfaces, not only changed files.
- Verify the regression matrix covers each affected surface and at least one boundary/failure case.
- Identify remaining unsafe patterns with concrete files/functions.

Output:
Invariant audit: clean|findings
Invariant Surface Inventory coverage:
- Shared helper roots: clean|finding|out-of-scope because <reason>
- Public entrypoints: clean|finding|out-of-scope because <reason>
- Read surfaces: clean|finding|out-of-scope because <reason>
- Write/delete/overwrite surfaces: clean|finding|out-of-scope because <reason>
- Staging/publish/rollback surfaces: clean|finding|out-of-scope because <reason>
- Producer/consumer evidence boundaries: clean|finding|out-of-scope because <reason>
- Stale-state/idempotency boundaries: clean|finding|out-of-scope because <reason>
- Unchanged downstream consumers: clean|finding|out-of-scope because <reason>
Surfaces inspected:
- <surface>: clean|finding|out-of-scope because <reason>
Remaining findings:
- <severity> <problem> | <impact> | <requested fix/test>
```

If the invariant audit reports findings, return to Phase 6 with an invariant-closure prompt. If the audit only checked changed files for a high-risk or repeated class, treat it as invalid and rerun it with the full inventory. If clean, rerun the normal full Phase 4-style cross-review.

## Phase 7: Independent Final Review

Mandatory for every PR.

1. Ensure build/lint/tests pass and the latest cross-review round is clean.
2. Spawn a clean-context subagent for read-only final review. It must not edit files, invoke `codeagent-wrapper`, use skills, or spawn agents.
3. Provide PR number, branch, full SHA, diff scope/changed files, OpenSpec files, fixture level/risk packs, all cross-review round summaries, and fix summary.
4. Ask it to focus on error recovery, backward compatibility, test coverage vs `tasks.md`, and pre-existing consumers.
5. Convert critical/major findings into Phase 6 style fix prompts.
6. Commit each logical fix after verification and push.
7. Do not post PR comments until Phase 7 is complete.

## Phase 8: Evidence, CI, Merge Gate

Freeze SHA:

```bash
git status
git diff HEAD origin/<branch> --stat
FULL_SHA=$(git rev-parse HEAD)
```

Wait for non-evidence CI checks. Use long quiet waits calibrated to the repo's normal CI duration; do not repeatedly poll or stream `gh run watch` output into the chat. Prefer sparse checks such as a long `gh run watch --exit-status --interval 60` wait with low output, or a quiet sleep loop that prints only final success/failure.

If CI fails, fetch the failing job logs once and classify the repair:

- `ci-only`: formatting/lint churn, deterministic test timing/port adjustment, CI workflow/cache/install wiring, generated artifact freshness, or log/evidence plumbing that does not alter runtime/product behavior, public contracts, production config, security boundaries, or test semantics. Delegate the fix, run relevant local verification, push, and wait for CI. Do not rerun cross-review solely for this class.
- `semantic`: source behavior, public API/schema, production config, security/auth/path behavior, evidence meaning, or test assertion changes that alter product semantics. Return to Phase 5-6 and rerun the normal review gate after verification.

Keep CI-only repair serial and minimal. If a CI failure appears to require parallel code-writing or broad multi-module edits, reclassify it as `semantic` or normal Phase 5/6 work instead of using the CI-only bypass.

Generate evidence locally before posting:

1. Create a local PR body markdown file.
2. Create local evidence comment markdown files.
3. Create the Chinese work-summary markdown file.
4. Inspect with `sed -n` or equivalent before posting.
5. Check:
   - frozen SHA is the final `HEAD`;
   - comments do not present stale findings as current findings;
   - prior findings are clearly marked as resolved when included;
   - markdown paths/backticks cannot be executed by the shell;
   - comment count is intentional and not excessive;
   - no secrets, tokens, signed URLs, or private env values appear.
6. Post with `gh pr comment --body-file <file>` or file-based API calls. Do not pass multi-line markdown through command substitution.

Post evidence for the cross-review reports actually used to gate merge. Prefer one concise evidence bundle per review round plus Phase 7 final review when many rounds occurred. If repository policy requires one comment per reviewer or a fixed comment count, follow it only when it does not reduce the reviewer set required by Phase 4; otherwise use a consolidated evidence bundle. Include reviewer names, SHA, local report paths, findings/resolution status, and whether the latest round is clean:

```text
Reviewer agent: <name>
Review round: <round 1|follow-up round N after fixes>
Reviewed head SHA: <40-char>
Summary: <one line>
Findings:
- <bullet per finding, or "None.">
Resolution:
- <for follow-up rounds, state whether prior findings are closed and list any remaining findings>
```

Update PR body Agent Review section:

```text
- Reviewer agents used: <name1>, <name2>, ...
- Reviewed head SHA: `<40-char>`
- Review evidence: [Round 1 Review 1](<url-1>) | [Round 1 Review 2](<url-2>) | [Follow-up N Review 1](<url-n>) | ...
- OpenSpec change: `<change-name>`; fixture level: <none|compact|expanded>; selected risk packs: <list>
- Key findings addressed: <summary, including follow-up review closure>
```

If evidence check exists, verify SHA, bullet format, and PR body links; retry up to 3 times. If a comment posting script behaves unexpectedly, stop it, delete incorrect comments if safe, and switch to a smaller file-based evidence bundle before proceeding.

Post Chinese work-summary comment before the merge gate or pre-authorized auto-merge:

```markdown
## 工作情况说明（Merge 前）

- 关联 Issue：#<ISSUE#>
- PR：#<PR#>
- 冻结提交：`<FULL_SHA>`

### 背景与目标
- <本 PR 要解决的问题、验收边界>

### 本次具体改动
- <按模块/文件/行为列出主要改动>

### 测试与验证
- <本地 build/lint/test/check 命令和结果>
- <手工验证、真实数据验证、CI 状态，如适用>

### Review 与修复闭环
- 交叉审核结论：<按 round 汇总 Phase 4 初审和 Phase 6.5 后续复审，附 PR 评论链接摘要>
- Phase 6/7 修复：<如有，列出修复项；无则说明无阻塞问题>

### 兼容性、风险与已知限制
- <API/数据格式/迁移兼容性>
- <剩余风险或明确不覆盖范围>

### 维护者关注点
- <需要人工重点看的点；没有则写“无额外关注点”>
```

Then stop for explicit merge approval unless the user explicitly pre-authorized auto-merge for the run. When auto-merge is pre-authorized, merge only after final review is clean, required CI passes, PR evidence comments are posted, and the Chinese work-summary comment is posted; delete the merged branch when appropriate and continue to the next unblocked issue.

After approval:

```bash
gh pr merge <PR#> --merge --delete-branch
git checkout master && git pull
gh issue close <ISSUE#> --comment "Closed via merged PR #<PR#>. <summary>"
```

Report next unblocked issue if any, but do not auto-start it.
