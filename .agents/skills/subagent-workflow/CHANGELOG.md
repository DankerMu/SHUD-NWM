# Changelog

All notable changes to this skill will be documented in this file.
This project adheres to [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
