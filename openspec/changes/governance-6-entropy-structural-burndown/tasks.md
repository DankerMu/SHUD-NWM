## Issue Ownership

This change creates one Epic plus implementation-ready sub-issues. Each
sub-issue owns one module or document boundary, declares dependencies, and has a
focused verification slice. Documentation cleanup, frontend evidence work,
governance audit parser work, and orchestrator structural refactors must remain
separate PR boundaries.

## 1. Entropy Baseline

- [x] 1.1 Generate the current entropy report with
  `uv run --no-sync python scripts/governance/audit_repo_entropy.py --format json`.
- [x] 1.2 Write `.entropy-baseline/latest.json` after explicit maintainer
  request, including branch, commit, summary counts, module heatmap, high-spread
  patterns, and cleanup priorities.
- [ ] 1.3 Verify normal JSON/Markdown/hard-gate report commands do not modify
  `.entropy-baseline/latest.json`.
- [ ] 1.4 Add `scripts/governance/write_entropy_baseline.py` as the
  maintainer-only baseline write helper; it consumes audit JSON output or runs
  the audit internally without making report modes mutate the baseline.
- [ ] 1.5 Verify an explicit baseline replacement through
  `scripts/governance/write_entropy_baseline.py` archives an existing
  `.entropy-baseline/latest.json` before writing a new latest snapshot.

## 2. Current Route Authority Runbook Cleanup

- [ ] 2.1 Update `docs/runbooks/two-node-production-e2e-plan.md` so live browser
  proof uses `/` plus `/ops`; `/hydro-met` is only a legacy redirect smoke.
- [ ] 2.2 Update `docs/runbooks/two-node-deployment-overview.md` so node-27
  user-facing display wording names `/` as the single-map entrypoint.
- [ ] 2.3 Update `docs/runbooks/node-27-bringup-checklist.md` so executable
  browser steps use `/` and `/ops`, with old paths only in redirect checks.
- [ ] 2.4 Add historical/superseded banners to old MVP runbooks that preserve
  pre-M26 `/hydro-met` execution steps.
- [ ] 2.5 Add or update the route-authority check in
  `scripts/governance/audit_repo_entropy.py` for current docs/runbooks,
  covering `/hydro-met`, `/forecast`, `/meteorology`, `/flood-alerts`,
  `/basins/:id`, and `/segments/:id` with explicit allowlist classes for
  historical evidence, redirect aliases, and compatibility context.

## 3. Mocked/Live Evidence Boundary

- [ ] 3.1 Reconcile `apps/frontend/e2e/m11-routes.spec.ts` broad API mock
  classification so it is not treated as live display evidence; prefer
  `.mocked.spec.ts` naming or a mocked-labelled directory if that matches the
  audit allowlist semantics.
- [ ] 3.2 Reconcile both broad API mock registrations in
  `apps/frontend/e2e/monitoring.spec.ts` so they are mocked regression or are
  split into a proper live profile; do not treat retry/cancel/operator mocked
  UI checks as display_readonly live proof.
- [ ] 3.3 Confirm `test:e2e:live-display` still rejects broad API mocks and
  requires explicit `PLAYWRIGHT_LIVE_BASE_URL` and
  `PLAYWRIGHT_LIVE_API_BASE_URL`.
- [ ] 3.4 Harden `scripts/governance/audit_repo_entropy.py` broad mock detection
  so multiline `page.route(` plus `'**/api/v1/**'` registrations are detected.
- [ ] 3.5 Add focused audit tests for inline broad mocks, multiline broad mocks,
  mocked-labelled allowlist behavior, and live-labelled/unallowlisted behavior.
- [ ] 3.6 Clarify frontend evidence profiles in `docs/VALIDATION.md` so
  mocked-regression, preview, visual, and live-display lanes state whether API
  mocks are allowed and whether they can produce live receipts.

## 4. Artifact Ownership Control

- [ ] 4.1 Update `docs/governance/DOC_STATUS.md` artifact ownership wording so
  `.dockerignore` appears as the expected literal term.
- [ ] 4.2 Run entropy audit JSON and confirm the
  `agent-artifact-ownership-policy` gate-eligible finding is gone.

## 5. Scheduler Lease Extraction

- [ ] 5.1 Create `services/orchestrator/scheduler_lease.py` and move lease
  classes/helpers behind compatibility imports from `scheduler.py`.
- [ ] 5.2 Preserve CAS renew, atomic replace, live holder non-reclaim,
  cross-host TTL, lease heartbeat, and lease-lost mutation fence behavior.
- [ ] 5.3 Verify with focused scheduler lease tests and ruff.

## 6. Scheduler Candidate-State Extraction

- [ ] 6.1 Create `services/orchestrator/scheduler_state.py` for
  `CandidateStateDecision`, candidate-state filtering, legacy identity
  validation, manual retry, active Slurm, permanent/cancelled, and terminal
  success helpers.
- [ ] 6.2 Keep all evidence keys, schema versions, status/reason codes, and old
  aliases unchanged.
- [ ] 6.3 Preserve private helper compatibility from `scheduler.py` until tests
  and callers migrate.
- [ ] 6.4 Verify with focused candidate-state and retry scheduler tests.

## 7. Scheduler Discovery Extraction

- [ ] 7.1 Create `services/orchestrator/scheduler_discovery.py` for cycle
  discovery, completion checks, and backfill selection.
- [ ] 7.2 Preserve oldest-gap-first, later-gap defer, and empty-model legacy
  fallback behavior.
- [ ] 7.3 Verify with focused backfill and discovery tests.

## 8. Scheduler Candidate Construction Extraction

- [ ] 8.1 Create `services/orchestrator/scheduler_candidates.py` for
  `_build_candidates`, canonical readiness gating, fresh full-chain,
  zero-canonical, active Slurm sync, and duplicate exclusion behavior.
- [ ] 8.2 Keep `ProductionScheduler._build_candidates` as a compatibility shim
  for the first extraction.
- [ ] 8.3 Verify with focused canonical readiness, active Slurm sync, and
  candidate selection tests.

## 9. Scheduler Execution Extraction

- [ ] 9.1 Create `services/orchestrator/scheduler_execution.py` for forcing
  production, candidate cohort grouping, concurrent submit evidence, and
  execution orchestration helpers that do not own lease or candidate-state
  semantics.
- [ ] 9.2 Preserve `run_once` ordering and mutation fences.
- [ ] 9.3 Verify with focused forcing and concurrent candidate tests.

## 10. Scheduler Evidence Extraction

- [ ] 10.1 Create `services/orchestrator/scheduler_evidence.py` for pass
  evidence assembly, pre-execution evidence reservation/proof helpers, and
  bounded evidence serialization.
- [ ] 10.2 Preserve pre-execution evidence reservation ordering and evidence
  keys.
- [ ] 10.3 Verify with focused pre-execution evidence and startup reconcile
  tests.

## 11. Chain Types And Stage Catalog Extraction

- [ ] 11.1 Create `services/orchestrator/chain_types.py` for shared stage
  dataclasses, contexts, result types, and stable type aliases.
- [ ] 11.2 Create `services/orchestrator/chain_stages.py` for stage catalog and
  static stage definitions, with re-exports from `chain.py`.
- [ ] 11.3 Preserve existing import surfaces until tests and callers migrate.
- [ ] 11.4 Verify with focused orchestration-chain type/catalog tests and ruff.

## 12. Chain Stage Execution Extraction

- [ ] 12.1 Create `services/orchestrator/chain_stage_execution.py` for stage
  reserve/submit/bind/poll/resume substeps.
- [ ] 12.2 Preserve reserve-before-sbatch, lost-reservation skip, idempotency
  comments, bind-after-submit, startup reconcile, and manual retry terminal
  stage behavior.
- [ ] 12.3 Verify with focused orchestration-chain reservation and submission
  tests.

## 13. Chain Manifest Extraction

- [ ] 13.1 Create `services/orchestrator/chain_manifests.py` for model run
  assembly, manifest index generation, runtime manifest safe write/validate,
  and manifest serialization helpers.
- [ ] 13.2 Preserve manifest schema versions, identity fields, quality states,
  residual blockers, safe-write behavior, and publish-stage evidence.
- [ ] 13.3 Verify with focused manifest and publish stage tests.

## 14. Chain Array Accounting Extraction

- [ ] 14.1 Create `services/orchestrator/chain_array_accounting.py` and move
  array aggregation/accounting helpers only after stage execution and manifest
  extraction are stable.
- [ ] 14.2 Preserve partial-stage aggregation, task outcomes, downstream
  manifest reduction, and publish behavior.
- [ ] 14.3 Verify with focused array, accounting, partial failure, and manifest
  reduction tests.

## 15. Review-Fix And Epic Verification

- [ ] 15.1 Run `openspec validate governance-6-entropy-structural-burndown --strict --no-interactive`.
- [ ] 15.2 Run Stage 3 Design Consistency, Spec Completeness, and Tasks
  Executability reviews; fix and re-review until all three report no P0/P1.
- [ ] 15.3 For each implementation sub-issue, require PR review-fix loops until
  the final review evidence has no P0/P1 findings.
- [ ] 15.4 Run `PYTHONDONTWRITEBYTECODE=1 uv run --no-sync python scripts/governance/audit_repo_entropy.py --format json`.
- [ ] 15.5 Run `PYTHONDONTWRITEBYTECODE=1 uv run --no-sync python scripts/governance/audit_repo_entropy.py --format markdown`.
- [ ] 15.6 Run `PYTHONDONTWRITEBYTECODE=1 uv run --no-sync python scripts/governance/audit_repo_entropy.py --mode hard-gate --format json` and confirm stdout remains parseable and the baseline file is not modified by the command.
- [ ] 15.7 For docs cleanup, run route-authority grep over current docs/runbooks
  and confirm old-route mentions are historical, redirect, or compatibility
  context.
- [ ] 15.8 For frontend evidence cleanup, run `cd apps/frontend && corepack pnpm
  test` and the relevant mocked/live Playwright command for the issue scope.
- [ ] 15.9 For each orchestrator extraction issue, run its focused pytest slice
  plus `uv run --no-sync ruff check services/orchestrator tests/test_production_scheduler.py tests/test_orchestration_chain.py tests/test_scheduler_backfill.py tests/test_retry.py tests/test_retry_cancel_consistency.py`.
- [ ] 15.10 Before closing the epic, run the broader affected suite:
  `uv run --no-sync pytest -q tests/test_production_scheduler.py tests/test_orchestration_chain.py tests/test_scheduler_backfill.py tests/test_retry.py tests/test_retry_cancel_consistency.py`.
- [ ] 15.11 Before closing the epic, run a final cross-review against the
  implemented issues and closure evidence; do not close while any P0/P1 remains.

## Stage 5 Issue Plan

### Epic: Governance-6 entropy structural burn-down

- Implementation Ready: yes.
- Ownership: Epic tracking only; no implementation code changes.
- In Scope: Track all sub-issues below, enforce dependency order, keep links to
  this OpenSpec change and `.entropy-baseline/latest.json`.
- Out of Scope: Direct source edits, merging implementation PRs without
  sub-issue review evidence, entropy hard-gate enablement.
- PR Boundary: No PR required unless the repository tracks issue templates or
  governance index updates.
- Dependencies: none.
- Acceptance: every sub-issue below is linked, every closed sub-issue has final
  no-P0/P1 review evidence, and tasks 15.1 through 15.11 are complete.

### G6-01 Entropy baseline report-only guard

- Implementation Ready: yes.
- Ownership: `.entropy-baseline/latest.json`,
  `scripts/governance/audit_repo_entropy.py`, `tests/test_entropy_audit_script.py`.
- In Scope: Keep the already-written baseline and prove normal JSON, Markdown,
  and hard-gate audit commands do not mutate it.
- Out of Scope: Baseline writer implementation, CI hard-gates, trend dashboard.
- Tasks: 1.3, 15.4, 15.5, 15.6.
- Dependencies: none.
- PR Boundary: Governance audit report-only verification only.
- Required Reading: `proposal.md`, `design.md`,
  `specs/governance-entropy-baseline/spec.md`,
  `.entropy-baseline/latest.json`.
- Acceptance: audit report commands leave `.entropy-baseline/latest.json`
  unchanged, stdout remains parseable, and entropy audit tests cover JSON,
  Markdown, and hard-gate report-only behavior.

### G6-02 Maintainer-only entropy baseline writer

- Implementation Ready: yes.
- Ownership: `.entropy-baseline/latest.json`,
  `scripts/governance/write_entropy_baseline.py`,
  `tests/test_entropy_audit_script.py`.
- In Scope: Add the maintainer-only baseline writer, preserve explicit write
  semantics, and archive an existing latest baseline before replacement.
- Out of Scope: Changing normal `audit_repo_entropy.py` report modes to write
  the baseline, CI hard-gates, trend dashboard.
- Tasks: 1.4, 1.5.
- Dependencies: G6-01.
- PR Boundary: Baseline writer helper and archive replacement tests only.
- Required Reading: `proposal.md`, `design.md`,
  `specs/governance-entropy-baseline/spec.md`,
  `.entropy-baseline/latest.json`.
- Acceptance: `scripts/governance/write_entropy_baseline.py` writes
  `.entropy-baseline/latest.json` only when explicitly run, archives a previous
  latest snapshot under `.entropy-baseline/<timestamp>.json`, and does not make
  JSON/Markdown/hard-gate audit report commands mutate the baseline.

### G6-03 Current route-authority runbooks

- Implementation Ready: yes.
- Ownership: `docs/runbooks/`.
- In Scope: Update current runbooks so `/` plus `/ops` are active browser proof,
  classify `/hydro-met`, `/forecast`, `/meteorology`, `/flood-alerts`,
  `/basins/:id`, and `/segments/:id` as historical, redirect, or compatibility
  context, and add historical banners to old MVP runbooks.
- Out of Scope: Frontend route code, Playwright implementation, orchestrator
  code.
- Tasks: 2.1, 2.2, 2.3, 2.4.
- Dependencies: none.
- PR Boundary: Docs-only current/historical route authority cleanup.
- Required Reading: `specs/evidence-boundary-hardening/spec.md`,
  `docs/governance/DOC_STATUS.md`.
- Acceptance: route-authority grep finds no active-looking legacy display route
  instructions in current runbooks.

### G6-04 Route-authority governance grep

- Implementation Ready: yes.
- Ownership: `scripts/governance/audit_repo_entropy.py`,
  `tests/test_entropy_audit_script.py`.
- In Scope: Add or update audit validation that classifies legacy route mentions
  as historical evidence, redirect aliases, compatibility context, or drift.
- Out of Scope: Editing runbook prose except fixtures needed by tests.
- Tasks: 2.5, 15.7.
- Dependencies: G6-03.
- PR Boundary: Route-authority audit check and tests only.
- Required Reading: `specs/evidence-boundary-hardening/spec.md`,
  `docs/governance/DOC_STATUS.md`, `apps/frontend/src/App.tsx`.
- Acceptance: check covers all six legacy route forms and fails on active
  current-runbook usage outside the allowlist classes.

### G6-05 Frontend mocked/live spec classification

- Implementation Ready: yes.
- Ownership: `apps/frontend/e2e/m11-routes.spec.ts`,
  `apps/frontend/e2e/monitoring.spec.ts`, frontend Playwright configuration
  needed for classification.
- In Scope: Reclassify or split broad API mocked regression specs so they
  cannot be treated as live display evidence; confirm live-display profile
  rejects broad API route mocks.
- Out of Scope: Governance audit parser changes, `docs/VALIDATION.md`,
  backend API behavior.
- Tasks: 3.1, 3.2, 3.3, 15.8.
- Dependencies: none.
- PR Boundary: Frontend tests/config only.
- Required Reading: `specs/evidence-boundary-hardening/spec.md`,
  frontend e2e config.
- Acceptance: frontend tests pass for the affected specs, live-display profile
  still requires explicit live base URLs and no broad API mocks.

### G6-06 Broad mock detector hardening

- Implementation Ready: yes.
- Ownership: `scripts/governance/audit_repo_entropy.py`,
  `tests/test_entropy_audit_script.py`.
- In Scope: Detect multiline `page.route(` plus `'**/api/v1/**'`, preserve
  mocked-labelled allowlist behavior, and add inline/multiline/live-looking
  test coverage.
- Out of Scope: Frontend spec renames, validation prose.
- Tasks: 3.4, 3.5, 15.4.
- Dependencies: none.
- PR Boundary: Governance audit parser and tests only.
- Required Reading: `specs/evidence-boundary-hardening/spec.md`,
  `docs/governance/entropy-budget.md`.
- Acceptance: audit tests cover inline broad mocks, multiline broad mocks,
  mocked-labelled allowlist behavior, and live-labelled/unallowlisted behavior.

### G6-07 Frontend evidence profile documentation

- Implementation Ready: yes.
- Ownership: `docs/VALIDATION.md`.
- In Scope: Define mocked-regression, preview, visual, and live-display lanes,
  including whether API mocks are allowed and whether receipts can be live
  evidence.
- Out of Scope: Frontend spec/config changes and audit parser changes.
- Tasks: 3.6.
- Dependencies: G6-05, G6-06.
- PR Boundary: Validation documentation only.
- Required Reading: `specs/evidence-boundary-hardening/spec.md`.
- Acceptance: docs clearly state broad API mocks cannot produce live display
  receipts.

### G6-08 Artifact ownership literal

- Implementation Ready: yes.
- Ownership: `docs/governance/DOC_STATUS.md`.
- In Scope: Add the literal `.dockerignore` ownership term expected by the
  audit and verify the finding is gone.
- Out of Scope: Broader governance documentation rewrite.
- Tasks: 4.1, 4.2.
- Dependencies: none.
- PR Boundary: Governance doc wording only.
- Required Reading: `specs/evidence-boundary-hardening/spec.md`,
  `docs/governance/DOC_STATUS.md`.
- Acceptance: entropy audit no longer reports the
  `agent-artifact-ownership-policy` gate-eligible finding.

### G6-09 Scheduler lease extraction

- Implementation Ready: yes.
- Ownership: `services/orchestrator/scheduler.py`,
  `services/orchestrator/scheduler_lease.py`, focused scheduler lease tests.
- In Scope: Extract lease classes/helpers with compatibility imports and
  preserve CAS renew, atomic replace, live-holder non-reclaim, cross-host TTL,
  heartbeat, and lease-lost mutation fence behavior.
- Out of Scope: Candidate-state, discovery, execution, evidence, chain, or
  reservation protocol rewrites.
- Tasks: 5.1, 5.2, 5.3.
- Dependencies: none.
- PR Boundary: Scheduler lease module extraction only.
- Required Reading: `specs/orchestrator-structural-burndown/spec.md`,
  `tests/test_production_scheduler.py`.
- Verification: `uv run --no-sync pytest -q tests/test_production_scheduler.py`
  plus `uv run --no-sync ruff check services/orchestrator tests/test_production_scheduler.py`.
- Acceptance: focused lease tests pass and `ruff check services/orchestrator`
  passes.

### G6-10 Scheduler candidate-state extraction

- Implementation Ready: yes.
- Ownership: `services/orchestrator/scheduler.py`,
  `services/orchestrator/scheduler_state.py`, focused candidate-state/retry
  tests.
- In Scope: Extract `CandidateStateDecision`, candidate-state filtering, legacy
  identity validation, manual retry, active Slurm, permanent/cancelled, and
  terminal success helpers.
- Out of Scope: Discovery, candidate construction, execution, chain stage
  behavior.
- Tasks: 6.1, 6.2, 6.3, 6.4.
- Dependencies: G6-09.
- PR Boundary: Scheduler state helpers and shims only.
- Required Reading: `specs/orchestrator-structural-burndown/spec.md`,
  `tests/test_retry.py`, `tests/test_retry_cancel_consistency.py`.
- Verification: `uv run --no-sync pytest -q tests/test_retry.py tests/test_retry_cancel_consistency.py`
  plus `uv run --no-sync ruff check services/orchestrator tests/test_retry.py tests/test_retry_cancel_consistency.py`.
- Acceptance: evidence keys, status/reason codes, schema versions, and old
  aliases remain unchanged; focused candidate-state and retry tests pass.

### G6-11 Scheduler discovery extraction

- Implementation Ready: yes.
- Ownership: `services/orchestrator/scheduler.py`,
  `services/orchestrator/scheduler_discovery.py`, backfill/discovery tests.
- In Scope: Extract cycle discovery, completion checks, and backfill selection.
- Out of Scope: Candidate construction, execution, evidence, chain behavior.
- Tasks: 7.1, 7.2, 7.3.
- Dependencies: G6-10.
- PR Boundary: Scheduler discovery/backfill helpers only.
- Required Reading: `specs/orchestrator-structural-burndown/spec.md`,
  `tests/test_scheduler_backfill.py`.
- Verification: `uv run --no-sync pytest -q tests/test_scheduler_backfill.py`
  plus `uv run --no-sync ruff check services/orchestrator tests/test_scheduler_backfill.py`.
- Acceptance: oldest-gap-first, later-gap defer, and empty-model legacy fallback
  behavior remain unchanged.

### G6-12 Scheduler candidate construction extraction

- Implementation Ready: yes.
- Ownership: `services/orchestrator/scheduler.py`,
  `services/orchestrator/scheduler_candidates.py`, candidate selection tests.
- In Scope: Extract `_build_candidates`, canonical readiness gating, fresh
  full-chain, zero-canonical, active Slurm sync, and duplicate exclusion
  behavior with a `ProductionScheduler._build_candidates` shim.
- Out of Scope: Lease, state, discovery, execution/evidence modules beyond
  imports needed by this extraction.
- Tasks: 8.1, 8.2, 8.3.
- Dependencies: G6-11.
- PR Boundary: Scheduler candidate construction only.
- Required Reading: `specs/orchestrator-structural-burndown/spec.md`,
  `tests/test_production_scheduler.py`.
- Verification: `uv run --no-sync pytest -q tests/test_production_scheduler.py`
  plus `uv run --no-sync ruff check services/orchestrator tests/test_production_scheduler.py`.
- Acceptance: focused canonical readiness, active Slurm sync, and candidate
  selection tests pass.

### G6-13 Scheduler execution extraction

- Implementation Ready: yes.
- Ownership: `services/orchestrator/scheduler.py`,
  `services/orchestrator/scheduler_execution.py`, forcing/concurrency tests.
- In Scope: Extract forcing production, candidate cohort grouping, concurrent
  submit evidence, and execution orchestration helpers that do not own lease or
  candidate-state semantics.
- Out of Scope: Evidence serialization helper extraction and chain stage
  behavior.
- Tasks: 9.1, 9.2, 9.3.
- Dependencies: G6-12.
- PR Boundary: Scheduler execution helpers only.
- Required Reading: `specs/orchestrator-structural-burndown/spec.md`,
  `tests/test_production_scheduler.py`.
- Verification: `uv run --no-sync pytest -q tests/test_production_scheduler.py`
  plus `uv run --no-sync ruff check services/orchestrator tests/test_production_scheduler.py`.
- Acceptance: `run_once` ordering and mutation fences remain stable; focused
  forcing/concurrent candidate tests pass.

### G6-14 Scheduler evidence extraction

- Implementation Ready: yes.
- Ownership: `services/orchestrator/scheduler.py`,
  `services/orchestrator/scheduler_evidence.py`, evidence/startup reconcile
  tests.
- In Scope: Extract pass evidence assembly, pre-execution evidence reservation
  and proof helpers, and bounded evidence serialization.
- Out of Scope: Execution orchestration and chain behavior.
- Tasks: 10.1, 10.2, 10.3.
- Dependencies: G6-13.
- PR Boundary: Scheduler evidence helpers only.
- Required Reading: `specs/orchestrator-structural-burndown/spec.md`,
  `tests/test_production_scheduler.py`.
- Verification: `uv run --no-sync pytest -q tests/test_production_scheduler.py`
  plus `uv run --no-sync ruff check services/orchestrator tests/test_production_scheduler.py`.
- Acceptance: pre-execution evidence reservation order and evidence keys remain
  stable; focused evidence/startup reconcile tests pass.

### G6-15 Chain types and stage catalog extraction

- Implementation Ready: yes.
- Ownership: `services/orchestrator/chain.py`,
  `services/orchestrator/chain_types.py`,
  `services/orchestrator/chain_stages.py`, focused chain type/catalog tests.
- In Scope: Extract shared stage dataclasses, contexts, result types, stable
  type aliases, stage catalog, and static definitions with re-exports.
- Out of Scope: Stage reserve/submit/bind/poll execution and manifest helpers.
- Tasks: 11.1, 11.2, 11.3, 11.4.
- Dependencies: G6-14.
- PR Boundary: Chain static type/catalog extraction only.
- Required Reading: `specs/orchestrator-structural-burndown/spec.md`,
  `tests/test_orchestration_chain.py`.
- Verification: `uv run --no-sync pytest -q tests/test_orchestration_chain.py`
  plus `uv run --no-sync ruff check services/orchestrator tests/test_orchestration_chain.py`.
- Acceptance: existing import surfaces keep working and focused chain catalog
  tests pass.

### G6-16 Chain stage execution extraction

- Implementation Ready: yes.
- Ownership: `services/orchestrator/chain.py`,
  `services/orchestrator/chain_stage_execution.py`, reservation/submission
  tests.
- In Scope: Extract stage reserve/submit/bind/poll/resume substeps and preserve
  reserve-before-sbatch, lost-reservation skip, idempotency comments,
  bind-after-submit, startup reconcile, and manual retry terminal behavior.
- Out of Scope: Manifest/model-run assembly and array accounting extraction.
- Tasks: 12.1, 12.2, 12.3.
- Dependencies: G6-15.
- PR Boundary: Chain stage execution only.
- Required Reading: `specs/orchestrator-structural-burndown/spec.md`,
  `tests/test_orchestration_chain.py`.
- Verification: `uv run --no-sync pytest -q tests/test_orchestration_chain.py`
  plus `uv run --no-sync ruff check services/orchestrator tests/test_orchestration_chain.py`.
- Acceptance: focused reservation and submission tests pass without duplicate
  Slurm submission behavior changes.

### G6-17 Chain manifest extraction

- Implementation Ready: yes.
- Ownership: `services/orchestrator/chain.py`,
  `services/orchestrator/chain_manifests.py`, manifest/publish tests.
- In Scope: Extract model run assembly, manifest index generation, runtime
  manifest safe write/validate, manifest serialization helpers, and
  publish-stage evidence helpers.
- Out of Scope: Array accounting aggregation and stage execution.
- Tasks: 13.1, 13.2, 13.3.
- Dependencies: G6-16.
- PR Boundary: Chain manifest helpers only.
- Required Reading: `specs/orchestrator-structural-burndown/spec.md`,
  `tests/test_orchestration_chain.py`.
- Verification: `uv run --no-sync pytest -q tests/test_orchestration_chain.py`
  plus `uv run --no-sync ruff check services/orchestrator tests/test_orchestration_chain.py`.
- Acceptance: manifest schema versions, identity fields, quality states,
  residual blockers, safe-write behavior, and publish-stage evidence remain
  stable.

### G6-18 Chain array accounting extraction

- Implementation Ready: yes.
- Ownership: `services/orchestrator/chain.py`,
  `services/orchestrator/chain_array_accounting.py`, focused array/accounting
  tests.
- In Scope: Move array aggregation/accounting helpers into
  `chain_array_accounting.py` after stage execution and manifest extraction are
  stable.
- Out of Scope: New scheduler behavior, reservation protocol changes, manifest
  schema changes, and manifest helper extraction except imports/call-site
  wiring needed to call `chain_array_accounting.py`.
- Tasks: 14.1, 14.2, 14.3.
- Dependencies: G6-17.
- PR Boundary: Chain array accounting helpers only.
- Required Reading: `specs/orchestrator-structural-burndown/spec.md`,
  `tests/test_orchestration_chain.py`.
- Verification: `uv run --no-sync pytest -q tests/test_orchestration_chain.py`
  plus `uv run --no-sync ruff check services/orchestrator tests/test_orchestration_chain.py`.
- Acceptance: partial-stage aggregation, task outcomes, downstream manifest
  reduction, and publish behavior remain unchanged.

### G6-19 Epic final review-fix closure

- Implementation Ready: yes.
- Ownership: GitHub issue closure evidence and OpenSpec change verification.
- In Scope: Run final affected suite, gather issue evidence, and perform
  cross-review until no P0/P1 remains.
- Out of Scope: New implementation changes except fixes required by final
  review.
- Tasks: 15.1, 15.2, 15.3, 15.10, 15.11.
- Dependencies: G6-01 through G6-18.
- PR Boundary: No implementation PR unless final review finds required fixes.
- Required Reading: all specs in this change and linked sub-issue evidence.
- Acceptance: OpenSpec strict validation passes, broader affected suite passes,
  and final cross-review reports no P0/P1 findings.
