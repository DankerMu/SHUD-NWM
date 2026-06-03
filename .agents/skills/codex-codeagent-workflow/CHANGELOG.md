# Changelog

All notable changes to this skill will be documented in this file.
This project adheres to [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
