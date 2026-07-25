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
   2. If it is absent, run the one-time **profile bootstrap** (Phase 0.0 below), which always writes `openspec/project-profile.md`, then load it.
   3. Bootstrap always persists a file: when it cannot infer anything project-specific it still writes `openspec/project-profile.md`, stamped as **Generic** with a note that nothing project-specific was found (the shared `project-profiles.md` Generic section is the template, not an in-memory-only fallback).
   Then load `issue-risk-contract.md`.
7. Announce the DAG sketch, the active project profile, and whether the OpenSpec change exists.

### Phase 0.0: Profile Bootstrap (one-time per project)

Run only when `openspec/project-profile.md` is missing. The shared
`project-profiles.md` provides the Generic default and SHUD/rSHUD/AutoSHUD
example templates to copy from; the active profile is project-local and survives
skill reinstalls because it lives under `openspec/`, not inside the skill.

1. Scan the repo for risk-bearing structure: primary language/build system, public entrypoints (CLI, API, services), data schemas/formats/serializers, external integrations and credentials, persisted/shared state, and shared helper roots — plus the command entry points (package scripts, Makefile/justfile targets, CI workflow steps) for setup/check/lint/typecheck/test/build.
2. Match against `project-profiles.md`. If an example profile fits (e.g. an AutoSHUD repo), copy it as the starting point; otherwise start from Generic. A pure-Generic bootstrap (nothing project-specific inferred) still produces a file: stamp it as Generic with a one-line note that nothing project-specific was found.
3. Always write `openspec/project-profile.md` with the eight profile fields: entry surfaces, contracts, risk axes, typical evidence, command entry points, verification matrix (surface -> command -> evidence), domain risk packs, domain expanded-triggers. Respect the size budget in `project-profiles.md`: short bullets not prose, never restate core packs/triggers, and stay under ~25 lines for a simple project or ~60 for a broad multi-subsystem system. Over-budget means it restates core or the repo should split into narrower profiles.
4. Note in the file that it is a living artifact maintained in Phase 0.5 as the project evolves.

The profile is a living document, not a one-shot. It does not change per issue, but it is updated whenever the project grows a new risk surface (see Phase 0.5 profile-gap maintenance).

## Phase 0.5: Risk Triage + OpenSpec Fixture

1. Create a short triage from issue, repo context, expected change surface, and the active project profile (`openspec/project-profile.md`).
   - Profile-gap maintenance: if the issue touches an entry surface, contract, risk axis, domain pack, or verification-matrix surface the profile does not yet describe, update `openspec/project-profile.md` before continuing. Keep the profile a living artifact; do not edit it for ordinary issues that already fit it.
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
   # artifact is required: proposal | design | specs | tasks - run once per artifact being authored
   openspec instructions <artifact> --change <change-name>
   ```
6. The orchestrator may directly edit `openspec/changes/<change>/**`; keep artifacts concise and focused. Author only the artifact set the fixture level requires (`issue-risk-contract.md` Fixture Level Rules): `none`/`compact` = `proposal.md` + `tasks.md` + one minimal spec delta (`design.md` exempt); `expanded` and above = full set.
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
10. Run one focused fixture review with a read-only `reviewer` subagent; it checks only fixture completeness. For high or broad-expanded fixtures, the review must specifically validate the `Invariant Matrix`; if it is absent or too vague, the fixture review is `revise`.
11. If review says `revise`, update the OpenSpec change and rerun the fixture review once.
12. Run `openspec validate <change-name> --strict --no-interactive`; do not proceed until it passes.

## Phase 1: Implementer Subagent Implementation

1. Ensure the `implementer` subagent is available in the orchestrator (Claude Code Task subagent or Codex subagent).
2. Create branch from integration base:
   ```bash
   DEFAULT_BRANCH=$(git symbolic-ref --quiet --short refs/remotes/origin/HEAD 2>/dev/null)
   DEFAULT_BRANCH=${DEFAULT_BRANCH#origin/}
   : "${DEFAULT_BRANCH:=$(git rev-parse --abbrev-ref HEAD)}"
   git checkout -b feat/issue-<N>-<change-name> "$DEFAULT_BRANCH"
   ```
3. Decide whether the implementation can be safely split into parallel code-writing slices. Use serial implementation for one shared state machine, one shared helper root, one schema/contract/public API boundary, uncertain design, or overlapping tests. If parallel implementation is safe, load and follow `parallel-worktree-delegation.md` before delegating any code-writing worker. The parent PR worktree remains the integration surface.
4. The implementer brief must include:
   - Required subagent boundary from `SKILL.md`.
   - Git ownership: implement and test only; the orchestrator owns commit, push, and PR — do not push or open/update PRs.
   - Issue reference, the OpenSpec files `proposal.md`, `design.md`, `tasks.md`, fixture level/risk packs, and key source files.
   - Repair intensity and any boundary-surface checklist from Phase 0.5.
   - The `Invariant Matrix` for high or broad-expanded work, with an instruction that implementation must preserve the governing invariant across every listed surface and report unchanged sibling surfaces inspected.
   - Clear scope and acceptance criteria.
   - Explicit instruction that implementation and tests ship together.
   - Full `tasks.md` test/evidence section.
   - Project verification commands.
   - Instruction to list changed files and verification results.
   - Instruction to report every deviation from the plan (fixture, `tasks.md`, or this brief) as one line each — what changed, why, which surfaces it touches: unexpected upstream/API behavior, a component that could not be reused as planned, a switched implementation path, or a constraint discovered mid-implementation. "No deviations" must be stated explicitly; an omitted deviation report is an incomplete result.
   - For high or broad-expanded work, instruction to report shared helper/boundary surfaces inspected, which were changed, and which were intentionally left unchanged with rationale.
   - For high or broad-expanded work, instruction to report regression evidence for every `Invariant Matrix` row, or explain why a row is out of scope only by citing the fixture.
5. Spawn either a single `implementer` subagent or the parallel worktree delegation defined by `parallel-worktree-delegation.md`, passing the implementation brief built above and the repository root as the working directory (Claude Code: a Task subagent; Codex: a subagent).
6. Use timeouts by complexity: simple 30 min, medium 1 h, complex 2 h. Do not kill a running implementer subagent. While it runs, wait silently with a long tool timeout; do not poll every few seconds or stream intermediate logs unless diagnosing a stuck/failing task. If it fails, refine the brief and retry, up to 2 attempts.

## Phase 2: Orchestrator Verification Only

Run the local CI-equivalent pipeline for the project as recorded in the active profile's command entry points and verification matrix (`openspec/project-profile.md`): execute the matrix rows mapped to the surfaces this change touches, plus the default build+test row. Fall back to discovering commands only when the profile predates the matrix or a row is missing — and then update the profile (Phase 0.5 profile-gap maintenance) so the next run consumes it instead of re-deriving. Example full pipelines:

- Node: `npm run build`, `npm test`
- Android: `cd packages/android && ./gradlew detekt ktlintMainSourceSetCheck ktlintTestSourceSetCheck ktlintAndroidTestSourceSetCheck testDebugUnitTest assembleDebug`
- R package: focused tests plus package check command appropriate to repo
- SHUD solver: build plus smallest relevant example/smoke run

Then do a read-only audit against the OpenSpec fixture:

- Error/failure paths have tests.
- The implementer's report includes the batched red-proof evidence for new-behavior tests (the red run output against pre-change source, per the implementer contract), and `git stash list` shows no leftover `red-proof` entry — a missing proof or a stranded stash is a finding.
- Selected risk packs are evidenced.
- CI-sensitive timers/processes are stable.
- Existing consumers and compatibility axes still work.

If any issue is found, create a precise Phase 6 style fix prompt. Do not patch implementation files directly.

A verification failure whose cause is not evident from the failing output is not yet a precise fix task: delegate a **diagnosis task** first (diagnosis brief in Phase 6). Guessing a fix from an undiagnosed failure is the exact anti-pattern the diagnosis gate exists to prevent.

## Phase 3: Commit and PR

1. Review `git status` and `git diff`.
2. Stage specific files only; never use `git add -A`.
3. Commit with conventional format: `feat(<scope>): <desc> (#<issue>)`.
4. Push branch.
5. Create PR. Leave Agent Review section empty until Phase 8. Seed a `偏离记录` section in the PR description from the implementer's reported deviations (write `无偏离` when none); Phase 6 fix passes append to this section so it stays the single running log of plan departures.

## Phase 4: Risk-Adaptive Cross-Review

Select reviewers from fixture level:

- `none`: skip unless Phase 2 audit finds risk.
- `compact`: run 1-2 reviewers focused on changed behavior and selected risk packs.
- `expanded`: run 2-4 reviewers; use all 4 for shared entrypoints, file/schema/publish behavior, solver/runtime behavior, or legacy compatibility.
- `high` or `broad-expanded`: use all 4 standard reviewers (Correctness, Integration, Security/Performance, Test & Evidence Coverage). Escalate to 6 reviewers when the PR touches DB-backed state, retry/cancellation, publish/delete/rollback, schema/evidence contracts, security boundaries, production config, or shared helper/state-machine roots. The two additional reviewers are `Spec Compliance` and `Invariant / State Machine / Compatibility`.
- Initial round only: if repository policy requires a fixed number of evidence comments, follow it only when it does not conflict with the six-reviewer high-risk escalation defined in `phase-flow.md` Phase 4; otherwise post a consolidated evidence bundle rather than reducing reviewer coverage.

Before spawning any comprehensive round's reviewers (initial or Phase 6.5 rerun), run the packaged evidence-hygiene linter `scripts/evidence_check.py`, passing the current PR body draft and evidence manifest via `--file`. A non-zero exit means the orchestrator's own bookkeeping is stale — unreplaced template placeholders, a current/frozen-head SHA claim that does not match HEAD, or a `Round N pending` claim the ledger already recorded. Fix the bookkeeping directly before spawning: it is orchestrator paperwork, not an implementer fix task, and it consumes no review round and gets no ledger line. Reviewer-authored reports are exempt from the placeholder scan by design; a review round is the most expensive linter there is, so reviewers get only what this script cannot judge.

Include the PR description's `偏离记录` section in every reviewer brief: deviations are where the implementer made choices the plan did not cover, so review attention goes there first. Use `phase-4-cross-review.md` to build the parallel reviewer-subagent briefs. Prefer spawning the full reviewer set as parallel subagents in one batch (Claude Code: multiple Task calls in one message; Codex: parallel subagents). Reviewer subagents are read-only and return their complete reports as their final messages; the orchestrator collects each returned report and persists it to `<REVIEW_DIR>/<report file>` (default `<REVIEW_DIR>` = `.workplans/<issue-or-pr>/review/`). Do not post PR comments in this phase.

Review rounds:

- Round 1 uses the risk-adaptive reviewer count above.
- After a Phase 6 fix pass, rerun cross-review before Phase 7 on the current head (classified exceptions rerun only their own gates: Phase 8 `ci-only` repairs and Phase 7 `local-repair` fixes). Post-fix rounds default to the **pinned risk-pack core only** (typically 2-3 reviewers): the lenses selected by the fixture's risk packs are pinned, present in every round, and carry the fix-regression recall. Free slots — complementary lenses from reviewer packs not yet used on this PR (`risk-adaptive-cross-review` `reviewer-packages.md`) — rotate back in only when the previous round had a critical/major finding or a failure-class repeat (ledger `repeats prior class: yes`); rotation is additive and never rotates out a pinned lens. This is a cost governor: comprehensive rounds are the most expensive unit in the workflow, so lens breadth is bought only when the ledger signals it. Record each round's lens mix in the evidence bundle; the policy is adjudicated by the review-loop log's lens attribution (Phase 8), keep/cut via the existing ADR mechanism.
- Every post-fix round must still cover the full updated PR diff and OpenSpec fixture — a prior round can miss issues outside the fix area — but coverage is structured for cost: exactly one reviewer per round keeps full-PR comprehensive scope, and the remaining reviewers focus on the fix delta, its blast radius, and regression surfaces while retaining full-diff access. Never run a round with zero full-scope reviewers.
- A cross-review round is clean only when it has no actionable findings. Critical/major findings and test coverage gaps always return to Phase 5-6. Minor findings must be fixed or explicitly deferred with issue/OpenSpec/user-instruction basis. When the `issue-scribe` agent is installed, "deferred with issue basis" means delegating the deferred finding to `issue-scribe` (it verifies, dedups, and files the tracked issue) and recording the returned issue URL in the evidence bundle; a deferral with neither an issue URL nor a recorded reason is not a valid deferral.
- When a comprehensive cross-review round comes back clean, record `Last clean reviewed SHA: <sha>` in the evidence bundle. This recorded SHA — not the frozen final HEAD — is the rollback anchor the Phase 8 pre-merge gate resets to if a later fix round corrupts a clean reviewed state.
- **Round ledger bookkeeping is mandatory**: every comprehensive round ends by appending its ledger line, and the next action is chosen from that line — format, counter semantics, CLI mechanics, and skip-block rule in `gates.md` (Round Ledger).
- A finding is actionable only if it satisfies the finding contract defined in the `risk-adaptive-cross-review` skill (`finding-contract.md`): severity, failure class, violated invariant/contract, concrete scenario, evidence, fix direction, required test/proof, sibling surfaces, and blocking status. Treat vague concerns, style preferences, and untestable possibilities as non-blocking notes unless the orchestrator can complete the missing fields from the diff and fixture.
- If a follow-up round finds the same failure class in another module, helper, or sibling surface, treat that as an invariant miss, not as a new isolated finding. Trigger Phase 6.2 before issuing the next fix prompt.
- Gate triggers (three-round, working-day, same-invariant), the no-trend-exemption rule, and everything that follows a trigger are governed by `gates.md` (Gate Table). Healthy convergence is common and is not a pathology signal — but it is claimed inside the persisted retro, never before it.
- While reviewers run, use the same silent long-wait rule as Phase 1. Avoid verbose `tail`, `watch`, or frequent status polling unless a reviewer fails or exceeds the expected timeout.
- If parallel review tooling fails without producing reports, diagnose the subagent failure once, then re-spawn the same reviewer-subagent set in parallel. Do not count a failed no-report invocation as a comprehensive review round.
- Phase 4 reviewers emit candidate findings, not final merge-blocking verdicts. Do not feed reviewer reports straight into Phase 5; they must pass the Phase 4.5 verification gate first.

## Phase 4.5: Independent Finding Verification Gate

Reviewers are recall-biased producers; this gate separates finding from judging so false positives never spawn fix rounds or inflate the round budget.

Steps:

1. Collect every candidate finding from the Phase 4 reports actually run on the current head.
2. Dedup near-duplicates: same defect + same location + same root reason collapse to one candidate. Merge the cited sibling surfaces and keep the highest severity.
3. Group the deduped candidates by failure class (the Phase 5 failure-class vocabulary), then run one parallel `verifier` subagent pass using the verifier template in `phase-4-cross-review.md`: one `verifier` subagent per failure-class batch, at most 5 candidates per batch — split larger classes into multiple batches; a singleton class is a batch of one. Batching by class is deliberate: same-class siblings share an evidence base, so one verifier seeing them together produces more consistent verdicts and catches near-duplicates the dedup missed. A verifier must not be a reviewer that produced any candidate in its batch, and the orchestrator must not self-adjudicate in place of a verifier.
4. Each verifier adjudicates every candidate in its batch independently and returns exactly one verdict per candidate — a batch-level verdict without per-candidate evidence is invalid and the batch must be rerun:
   - `CONFIRMED`: the failing scenario is constructible from the diff/fixture/contracts.
   - `PLAUSIBLE`: reachable but not fully constructible — rare error paths, falsy-zero treated as missing, off-by-one at a non-excluded boundary, races, retry storms, stale cache/DB rows, regex/allowlist that lost an anchor.
   - `REFUTED`: factually wrong (quote the line), provably impossible (cite type/constant/invariant), already handled in this diff (cite the guard), or pure style with no observable effect.
5. Apply the fixture-level bias:
   - `high` / `broad-expanded`: CONFIRMED and PLAUSIBLE are both merge-blocking inputs to Phase 5 (recall-biased).
   - `expanded`: CONFIRMED is merge-blocking; PLAUSIBLE is merge-blocking only when it maps to a selected risk pack or Invariant Matrix row, otherwise a non-blocking note.
   - `low` / `compact`: only CONFIRMED is merge-blocking; PLAUSIBLE becomes a non-blocking note (precision-biased).
6. Drop every REFUTED candidate, recording one line per dropped candidate: `<candidate> -> REFUTED: <verifier rationale>`. A `wontfix` test-coverage exception remains forbidden: test coverage gaps are never dropped this way. Persist the verdict table to the review evidence directory (`<REVIEW_DIR>`, default `.workplans/<issue-or-pr>/review/`).

Rules:

- This verification pass is not a comprehensive cross-review round. Do not increment the Phase 4 round counter for it.
- Never upgrade a REFUTED candidate back to blocking without a fresh reviewer finding on a later head.
- If verification tooling fails without producing verdicts, rerun it once; do not silently promote all candidates to blocking, and do not silently drop them.
- Verdicts must use only evidence from the diff, OpenSpec fixture, existing code/contracts, or tests. A verifier may not invent a scenario to confirm or a guard to refute.

## Phase 5: Fix Synthesis

Combine:

- OpenSpec fixture and selected risk packs
- Verified findings from Phase 4.5 (CONFIRMED plus risk-weighted PLAUSIBLE), including follow-up rounds after fixes
- Orchestrator read-only diff review
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

Drop or downgrade non-actionable review notes before fix planning unless the orchestrator can complete the actionable finding contract from the diff, fixture, and verification evidence.

Phase 4.5 already adjudicated each candidate. Do not re-litigate REFUTED candidates here. Treat the CONFIRMED and risk-weighted PLAUSIBLE set as the actionable input. The only additional filter at this stage is the `wontfix` rule below for findings the OpenSpec fixture, issue text, or explicit user instruction places out of scope; test coverage gaps are never `wontfix`.

Produce one checklist grouped by failure class. For each group include severity, file/line examples when available, violated invariant, concrete failing scenario, requested behavior, required test/evidence, selected risk pack, analogous surfaces that must be audited, and merge-blocking status. A group may cite multiple reviewer comments, but it should produce one class-level fix prompt.

Rules:

- `wontfix` must cite spec/design, issue text, OpenSpec non-goal, or explicit user instruction.
- Test coverage gaps are never `wontfix`.
- **Diagnosis gate (cause-unknown only)**: a finding or verification/CI failure whose cause is not established from the failing output enters the fix list only after a diagnosis task (Phase 6 diagnosis brief) has produced a **red-capable command already run at least once** (invocation + output pasted), a minimal repro, and a confirmed hypothesis with evidence. Ordinary findings with an exact file/line and evident cause skip this gate — no diagnosis tax on the first touch. A finding whose fix failed to close it in a prior round re-enters Phase 5 through this gate.
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
- If any ordinary-loop gate has triggered (`gates.md` Gate Table), write and persist the Review Failure Retro instead of another ordinary fix list — template, failure shapes, default actions, post-gate budgets, and CLI registration all in `gates.md`.
- Do not edit implementation files while synthesizing.

## Phase 6: Implementer Subagent Fix Pass

Before delegating fixes, decide whether parallel writing is safe. If any Phase 6 code-writing fix is parallelized, load and follow `parallel-worktree-delegation.md`. Use serial fixing for one shared state machine, one shared helper root, one schema/contract/public API boundary, uncertain design, or overlapping tests. Read-only audits may still run in parallel without worktrees.

Serial fix prompt template:

```text
<Required subagent boundary>

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
Report any deviation from this fix list (what/why/impact); state "no deviations" explicitly.
```

Append reported deviations to the PR description's `偏离记录` section before the next review round.

For cause-unknown fixes that entered through the Phase 5 diagnosis gate, the fix entry's `Test:` field must name the diagnosis's red command: run it red before the fix, green after, and promote the minimal repro into a regression test at a correct seam — if no correct seam exists, that is itself a finding; record it and route it through deferral. Temporary instrumentation (from diagnosis or fixing) must carry a unique `[DEBUG-<tag>]` prefix and be removed before commit; `grep -r "DEBUG-"` must come back clean.

Diagnosis brief template — delegated to an `implementer` subagent as a report-only task (no fix). Vocabulary is canonical in the `diagnosing-bugs` skill; the orchestrator inlines this distilled brief because leaf subagents do not invoke skills:

```text
<Required subagent boundary>

# Diagnosis for PR #<N>: <failure summary>

Symptom: <user-visible/CI-visible failure, quoted output>
Scope: <files/surfaces plausibly involved; issue + OpenSpec fixture references>

Discipline:
1. Build a feedback loop FIRST: one command that goes red on this failure.
   Prefer, in order: failing test at a reachable seam; curl/CLI + fixture diff;
   replay of a captured trace; throwaway harness (single service, mocked deps);
   bisect harness; differential run (old vs new / config A vs B). If you cannot
   build a red command, report what you tried and stop — do NOT hypothesise.
2. Minimise: cut inputs/callers/config/data one at a time until every remaining
   element is load-bearing (removing any one turns the loop green).
3. Hypothesise: 3-5 ranked, falsifiable hypotheses, each with a prediction
   ("if X is the cause, changing Y makes it disappear / Z makes it worse").
4. Instrument: one variable at a time, each probe mapped to one hypothesis;
   debugger/REPL over logs; tag every debug log with [DEBUG-<tag>]. For
   performance regressions: baseline measurement first, then bisect.

Report (this task delivers a diagnosis, never a fix):
- Red command: invocation + output, already run at least once
- Minimal repro description
- Ranked hypotheses, the confirmed one marked, with evidence
- Instrumentation left in place ([DEBUG-<tag>] list) for the fix task to remove
```

For pattern escalation or high-risk classes, replace the narrow fix list with an invariant-closure prompt:

```text
<Required subagent boundary>

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

Run Phase 2 verification after the implementer subagent returns. If verification passes, commit/push the fix. Before committing or finishing any Phase 6 fix round that used delegated worktrees, complete the cleanup checklist in `parallel-worktree-delegation.md` and persist any intentionally retained worktree path and reason in the evidence directory.

Before the post-fix cross-review after a pattern escalation, run Phase 6.2. Do not skip Phase 6.2 merely because local tests passed. Then proceed to Phase 6.5 to rerun cross-review on the fixed head, except for CI-only repairs classified in Phase 8.

## Phase 6.2: Invariant Audit

Run this phase when Phase 5 marks pattern escalation or when a high-risk selected pack has a reusable unsafe pattern.

The orchestrator may do the audit read-only, or delegate one read-only `reviewer` subagent audit. The audit must not edit files. It must answer:

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

If the invariant audit reports findings, return to Phase 6 with an invariant-closure prompt. If the audit only checked changed files for a high-risk or repeated class, treat it as invalid and rerun it with the full inventory. If clean, proceed to Phase 6.5 for the normal full Phase 4-style cross-review.

## Phase 6.5: Repeat Cross-Review After Fixes

After a Phase 6 fix pass (and Phase 6.2 when a pattern escalation triggered it), rerun a full Phase 4-style comprehensive cross-review on the current head, except for CI-only repairs classified in Phase 8 and `local-repair` fixes classified in Phase 7. Use the post-fix reviewer mix defined in Phase 4 review rounds: pinned-core default, signal-triggered free-slot rotation, one full-scope reviewer with the rest delta-focused. The round as a whole must still cover the full diff — a prior round can miss issues outside the fix area.

When the rerun round comes back clean, record `Last clean reviewed SHA: <sha>` in the evidence bundle as the rollback anchor (see the Phase 4 review-round rule).

Continue Phase 5-6-6.5 loops only while no ordinary-loop gate has triggered and until the latest comprehensive cross-review round is clean. Append each rerun round's ledger line before choosing the next action; the ledger's `gate` field — not the impression that findings are trending down — decides whether the loop may continue (`gates.md`; hard ceiling: 5 comprehensive rounds per PR, beyond which the only corrective action is a PR split). Count repeated same-class findings as invariant misses requiring another cross-cutting closure pass, not as fresh isolated issues. Escalate only for real blockers, contradictory requirements, missing tooling, or unresolved product/scope decisions.

## Phase 7: Independent Final Review

Mandatory for every PR.

1. Ensure build/lint/tests pass and the latest cross-review round is clean.
2. Spawn a clean-context `reviewer` subagent for read-only final review. It must not edit files, invoke this workflow, use skills, or spawn further subagents.
3. Provide PR number, branch, full SHA, diff scope/changed files, OpenSpec files, fixture level/risk packs, all cross-review round summaries, the verified-findings list, and fix summary.
4. Run this as the **Gap Sweep** defined in `risk-adaptive-cross-review` (`SKILL.md` → Synthesis): a fresh clean-slate pass with the already-verified findings visible, looking only for real defects not already listed — especially removed behavior never re-established, caller/callee contract drift, boundary/error/cleanup paths, async ordering and cancellation, cross-tenant/permission paths, migration/backfill, cache invalidation, and wrapper recursion. It must also confirm test coverage vs `tasks.md` and pre-existing consumer compatibility. Apply the Reject When precision gate; do not pad when the sweep is clean.
5. Classify each critical/major finding's fix before delegating (same spirit as the Phase 8 `ci-only`/`semantic` split):
   - `local-repair`: test-only or evidence-only additions, or a single-file local fix with a covering test that touches no contract, shared helper, `Invariant Matrix` surface, auth/path/publish behavior, or public API. For `high`/`broad-expanded` fixtures only test/evidence-only changes qualify — any source behavior change is `semantic`.
   - `semantic`: everything else.
6. `local-repair` path: delegate the fix (Phase 6 style prompt), run Phase 2 verification, commit/push, record the classification and rationale in the evidence bundle, then rerun the Phase 7 final review on the new head. No comprehensive cross-review round is required and the round counter does not move. At most two `local-repair` loops per PR; a third Phase 7 finding pass is `semantic` by definition — a gap sweep that keeps finding issues is a signal, not a nuisance.
7. `semantic` path: return to Phase 5-6; the post-fix Phase 6.5 comprehensive round applies (it increments the round counter and the three-round hard-gate machinery), then rerun Phase 7 on the new head.
8. Commit each logical fix after verification and push.
9. Do not post PR comments until Phase 7 is complete.

## Phase 8: Evidence, CI, Merge Gate

Freeze SHA:

```bash
git status
git diff HEAD origin/<branch> --stat
FULL_SHA=$(git rev-parse HEAD)
```

Wait for non-evidence CI checks. Use long quiet waits calibrated to the repo's normal CI duration; do not repeatedly poll or stream `gh run watch` output into the chat. Prefer sparse checks such as a long `gh run watch --exit-status --interval 60` wait with low output, or a quiet sleep loop that prints only final success/failure. When the `monitor` agent is installed, delegate the wait instead: spawn one `monitor` subagent with the concrete run/check IDs; it performs a single quiet blocking wait and returns only the terminal state (or a timeout report), keeping the orchestrator loop free of polling output.

If CI fails, fetch the failing job logs once and classify the repair:

- `ci-only`: formatting/lint churn, deterministic test timing/port adjustment, CI workflow/cache/install wiring, generated artifact freshness, or log/evidence plumbing that does not alter runtime/product behavior, public contracts, production config, security boundaries, or test semantics. Delegate the fix, run relevant local verification, push, and wait for CI. Do not rerun cross-review solely for this class.
- `semantic`: source behavior, public API/schema, production config, security/auth/path behavior, evidence meaning, or test assertion changes that alter product semantics. Return to Phase 5-6 and rerun the normal review gate after verification.

Keep CI-only repair serial and minimal. If a CI failure appears to require parallel code-writing or broad multi-module edits, reclassify it as `semantic` or normal Phase 5/6 work instead of using the CI-only bypass.

A CI failure that does not reproduce locally is not classified yet: run it through a diagnosis task first (Phase 6 diagnosis brief — typically a differential loop between the CI environment and local: dependency/tool versions, env vars, parallelism, timing, cache state), then classify `ci-only` vs `semantic` from the confirmed cause, not from the log's surface shape.

Generate evidence locally before posting:

1. Create a local PR body markdown file.
2. Create local evidence comment markdown files.
3. Create the Chinese work-summary markdown file.
4. Inspect with `sed -n` or equivalent before posting.
5. Check:
   - `scripts/evidence_check.py --file <each drafted body/comment file>` exits 0 (no unreplaced placeholders or TODO/TBD markers, current/frozen-head SHA claims match the frozen `HEAD`, no stale `Round N pending` lines);
   - frozen SHA is the final `HEAD`;
   - comments do not present stale findings as current findings;
   - prior findings are clearly marked as resolved when included;
   - markdown paths/backticks cannot be executed by the shell;
   - comment count is intentional and not excessive;
   - no secrets, tokens, signed URLs, or private env values appear.
6. Post with `gh pr comment --body-file <file>` or file-based API calls. Do not pass multi-line markdown through command substitution.

Post evidence for the cross-review reports actually used to gate merge. Prefer one concise evidence bundle per review round plus Phase 7 final review when many rounds occurred. If repository policy requires one comment per reviewer or a fixed comment count, follow it only when it does not reduce the reviewer set required by Phase 4; otherwise use a consolidated evidence bundle. Include reviewer names, SHA, local report paths (under `<REVIEW_DIR>`, default `.workplans/<issue-or-pr>/review/`), findings/resolution status, and whether the latest round is clean:

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

### 计划偏离
- <汇总 PR 描述 `偏离记录` section：每条 偏离点/原因/影响面；全程无偏离则写"无偏离">

### 测试与验证
- <本地 build/lint/test/check 命令和结果>
- <手工验证、真实数据验证、CI 状态，如适用>

### Review 与修复闭环
- 交叉审核结论：<按 round 汇总 Phase 4 初审和 Phase 6.5 后续复审，附 PR 评论链接摘要>
- Phase 6/7 修复：<如有，列出修复项；无则说明无阻塞问题>

### 兼容性、风险与已知限制
- <API/数据格式/迁移兼容性>
- <剩余风险或明确不覆盖范围：每条附 follow-up issue 链接（issue-scribe 产出），或一行不落 issue 的理由>

### 维护者关注点
- <需要人工重点看的点；没有则写“无额外关注点”>
```

### Pre-merge evidence hard-gate

Before requesting merge approval or running a pre-authorized auto-merge, verify all of the following for the frozen final HEAD. If any fails, do not merge — re-run the missing phase or fix the gap, and record a skip block for the accountability log:

- The review track is satisfied by EITHER (a) or (b):
  - **(a) SHA-matched review artifacts**, all present for the frozen final HEAD:
    - The PR `Agent Review` section lists the reviewer agents actually used and a `Reviewed head SHA` that is either the frozen `FULL_SHA` or the recorded `Last clean reviewed SHA` with every later commit recorded as a Phase 8 `ci-only` repair or Phase 7 `local-repair` fix (an unclassified commit after the clean SHA is a gate failure).
    - The Phase 4.5 verifier verdict tables for the comprehensive rounds actually run are persisted in the review evidence directory (`<REVIEW_DIR>`, default `.workplans/<issue-or-pr>/review/`).
    - The latest comprehensive cross-review round is clean (no actionable findings) at the recorded `Last clean reviewed SHA`, and the Phase 7 final review is complete on the frozen final HEAD.
  - **(b) "review not required" record**: the fixture risk tier is `none` and the Phase 2 audit found no risk, and that fact is persisted in the evidence bundle against the frozen `FULL_SHA`. This is the only path that legitimately skips Phase 4/4.5/7 (see Phase 4 `none` handling and the Phase 2 audit).
  - Missing both (a) and (b) is a skip block: do not merge, and record it for the accountability log.
- No posted evidence presents stale findings as current.
- **Deferral routing**: every explicitly deferred finding and every 剩余风险/已知限制 entry in the work summary carries either a tracked issue URL (delegated to `issue-scribe` when installed) or a recorded one-line reason why no issue is filed. An unrouted deferral is a gate failure, not a silent drop.
- **Completion self-audit (premature-completion guard)**: re-derive each issue acceptance criterion and each selected `tasks.md` item and confirm the diff/tests actually satisfy it — not "the agent said done". Use the project profile's verification matrix to map each touched surface to its verification command and expected evidence; a matrix row for a touched surface that was never executed on the final head counts as an uncovered criterion. Confirm no leftover edge/error path the fixture required is unhandled, and that the final changes are internally consistent (no two fixes that contradict each other). Any uncovered criterion blocks the merge and returns to Phase 5-6; it does not become a silent deferral.
- **Oracle integrity**: confirm the OpenSpec fixture, acceptance criteria, existing tests, and CI gates were not weakened, deleted, or rewritten to make the gate pass (`risk-adaptive-cross-review` → `finding-contract.md` Oracle Integrity). A test/spec change is legitimate only when it tracks a real contract change recorded in the OpenSpec change, never to silence a failure.

This gate mixes deterministic and judgment-based checks. The SHA-match and artifact-presence clauses above are deterministic: they either hold against the frozen final HEAD or they do not. The completion self-audit and oracle-integrity clauses are mandatory checkable procedures — enumerate each acceptance criterion and selected task and confirm the diff/tests satisfy it, and diff the test/spec/CI files against the reviewed baseline — that still require reviewer judgment. A failure of any clause, deterministic or judgment-based, blocks the merge rather than producing a "probably fine" pass.

The rollback anchor is the last clean reviewed SHA, not the frozen final HEAD. After each clean comprehensive cross-review round the orchestrator records `Last clean reviewed SHA: <sha>` in the evidence bundle (Phase 4 / Phase 6.5). If a late fix round corrupts a previously clean reviewed state, reset the branch to that recorded SHA and re-run the fix rather than layering patches on a broken head.

This gate is orchestrator-enforced by default. Where the host repo supports it, add a required CI/branch-protection status check that fails the merge unless the `Agent Review` evidence block is present and SHA-matched, so skipping the review/verify loop is a detectable hard action rather than discretionary. A portable skill cannot install that check; recommend it as host-repo setup and, until it exists, treat the orchestrator gate as the enforcement point and log every skip block.

Then stop for explicit merge approval unless the user explicitly pre-authorized auto-merge for the run. When auto-merge is pre-authorized, merge only after final review is clean, required CI passes, PR evidence comments are posted, and the Chinese work-summary comment is posted; delete the merged branch when appropriate and continue to the next unblocked issue.

After approval:

```bash
gh pr merge <PR#> --merge --delete-branch
DEFAULT_BRANCH=$(git symbolic-ref --quiet --short refs/remotes/origin/HEAD 2>/dev/null)
DEFAULT_BRANCH=${DEFAULT_BRANCH#origin/}
: "${DEFAULT_BRANCH:=main}"
git checkout "$DEFAULT_BRANCH" && git pull
gh issue close <ISSUE#> --comment "Closed via merged PR #<PR#>. <summary>"
```

When the gate CLI is in use, run `review_gate.py close` after the merge (or when abandoning the PR) so `.review-gate.json` is removed and the `review-gate` spawn fence returns to no-op.

### Cross-Run Loop Accountability

After a successful merge, append exactly one line to the host repo's committed, append-only review-loop log (`docs/review-loop-log.jsonl`, or the project's existing operational-log location). Commit it as merge follow-up, not inside the PR.

```json
{"issue":<N>,"pr":<N>,"date":"<merge-date>","fixture":"none|compact|expanded|high|broad-expanded","rounds":<comprehensive-rounds>,"gate_net_catch":<n>,"verdicts":{"confirmed":<n>,"plausible":<n>,"refuted":<n>},"residual_deferred":<n>,"premerge_skip_blocks":<n>,"round_lenses":[["<lens>","..."],["..."]],"catches":[{"round":<n>,"lens":"<lens>","class":"<failure class>","severity":"<sev>"}]}
```

- `gate_net_catch`: the accountability metric — count of verified findings (CONFIRMED plus merge-blocking PLAUSIBLE) the review/verify loop caught that local Phase 2 verification AND CI did not already surface. It measures the unique value the cross-review + verifier loop adds beyond the cheaper machine gates.
- `premerge_skip_blocks`: times the pre-merge evidence hard-gate had to block this PR for missing/stale evidence (the dim-8 skip-rate signal).
- `round_lenses`: the lens mix actually used per round (index 0 = round 1) — the instrumentation for the pinned-core + rotating-free-slots trial.
- `catches`: one entry per `gate_net_catch` finding — the round it surfaced, the lens (reviewer pack) that produced it, its failure class and severity. This attributes later-round catches to either the pinned core (fix-regression recall) or a rotated-in lens (blind-spot recall).

Rotation keep/cut criterion (same human-call mechanism as below, minimum sample ~8 merged multi-round PRs): if later-round catches concentrate in rotated-in lenses, rotation is buying real union recall — keep. If they come almost entirely from pinned-core lenses on fix-touched code, rotation buys nothing — record the cut in `docs/adr/` and revert follow-up rounds to the round-1 mix.

Keep / narrow criterion (a human call; minimum sample ~8 merged PRs at a given fixture level; **default to keep when in doubt**, because this workflow prioritizes correctness over cost):

- If `gate_net_catch` stays ≈ 0 across the sample for a fixture level, surface it for a recorded keep/cut decision in `docs/adr/` — narrow that level's reviewer set or drop a reviewer role that never produces a verified finding.
- If one reviewer role's candidates are almost always REFUTED, narrow or retune that role's brief rather than dropping coverage.
- Never auto-narrow; the log informs a deliberate, recorded decision.

This is organizational accountability — whether the review loop keeps earning its keep across PRs. Cross-run risk *learning* (a newly exposed risk surface) already ratchets through the living `openspec/project-profile.md` (Phase 0.0/0.5) and through committed regression tests, so resolved issues become durable invariants.

Report next unblocked issue if any, but do not auto-start it.
