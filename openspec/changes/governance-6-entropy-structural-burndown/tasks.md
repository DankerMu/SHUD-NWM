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
- [x] 1.3 Verify normal JSON/Markdown/hard-gate report commands do not modify
  `.entropy-baseline/latest.json`.
- [x] 1.4 Add `scripts/governance/write_entropy_baseline.py` as the
  maintainer-only baseline write helper; it consumes audit JSON output or runs
  the audit internally without making report modes mutate the baseline.
- [x] 1.5 Verify an explicit baseline replacement through
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
  covering `/overview`, `/hydro-met`, `/forecast`, `/meteorology`,
  `/flood-alerts`, `/basins/:id`, and `/segments/:id` with explicit allowlist
  classes for historical evidence, redirect aliases, and compatibility context.

## 3. Mocked/Live Evidence Boundary

- [ ] 3.1 Reconcile `apps/frontend/e2e/m11-routes.mocked.spec.ts` broad API mock
  classification so it is not treated as live display evidence; prefer
  `.mocked.spec.ts` naming or a mocked-labelled directory if that matches the
  audit allowlist semantics.
- [ ] 3.2 Reconcile both broad API mock registrations in
  `apps/frontend/e2e/monitoring.mocked.spec.ts` so they are mocked regression or are
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

- [x] 5.1 Create `services/orchestrator/scheduler_lease.py` and move lease
  classes/helpers behind compatibility imports from `scheduler.py`.
- [x] 5.2 Preserve CAS renew, atomic replace, live holder non-reclaim,
  cross-host TTL, lease heartbeat, and lease-lost mutation fence behavior.
- [x] 5.3 Verify with focused scheduler lease tests and ruff.

## 6. Scheduler Candidate-State Extraction

- [x] 6.1 Create `services/orchestrator/scheduler_state.py` for
  `CandidateStateDecision`, candidate-state filtering, legacy identity
  validation, manual retry, active Slurm, permanent/cancelled, and terminal
  success helpers.
- [x] 6.2 Keep all evidence keys, schema versions, status/reason codes, and old
  aliases unchanged.
- [x] 6.3 Preserve private helper compatibility from `scheduler.py` until tests
  and callers migrate.
- [x] 6.4 Verify with focused candidate-state and retry scheduler tests.

## 7. Scheduler Discovery Extraction

- [x] 7.1 Create `services/orchestrator/scheduler_discovery.py` for cycle
  discovery, completion checks, and backfill selection.
- [x] 7.2 Preserve oldest-gap-first, later-gap defer, and empty-model legacy
  fallback behavior.
- [x] 7.3 Verify with focused backfill and discovery tests.

## 8. Scheduler Candidate Construction Extraction

- [x] 8.1 Create `services/orchestrator/scheduler_candidates.py` for
  `_build_candidates`, canonical readiness gating, fresh full-chain,
  zero-canonical, active Slurm sync, and duplicate exclusion behavior.
- [x] 8.2 Keep `ProductionScheduler._build_candidates` as a compatibility shim
  for the first extraction.
- [x] 8.3 Verify with focused canonical readiness, active Slurm sync, and
  candidate selection tests.

## 9. Scheduler Execution Extraction

- [x] 9.1 Create `services/orchestrator/scheduler_execution.py` for forcing
  production, candidate cohort grouping, concurrent submit evidence, and
  execution orchestration helpers that do not own lease or candidate-state
  semantics.
- [x] 9.2 Preserve `run_once` ordering and mutation fences.
- [x] 9.3 Preserve scheduler runtime-root preflight semantics: missing
  `published_artifact_root` is a control publish-stage creatable root, while
  missing workspace, object-store, runtime, temp, lock, and evidence roots still
  block before registry, adapter, active-repository, or submission work.
- [x] 9.4 Verify with focused forcing, concurrent candidate, and runtime-root
  preflight tests.

## 10. Scheduler Evidence Extraction

- [x] 10.1 Create `services/orchestrator/scheduler_evidence.py` for pass
  evidence assembly, pre-execution evidence reservation/proof helpers, and
  bounded evidence serialization.
- [x] 10.2 Preserve pre-execution evidence reservation ordering and evidence
  keys.
- [x] 10.3 Verify with focused pre-execution evidence and startup reconcile
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
- Fixture evidence:
  - Existing `.entropy-baseline/latest.json` + JSON report command:
    `PYTHONDONTWRITEBYTECODE=1 uv run --no-sync python
    scripts/governance/audit_repo_entropy.py --format json` -> parseable JSON,
    `metadata.baseline_written == false`, and unchanged baseline bytes.
  - Existing `.entropy-baseline/latest.json` + Markdown report command:
    `PYTHONDONTWRITEBYTECODE=1 uv run --no-sync python
    scripts/governance/audit_repo_entropy.py --format markdown` -> Markdown
    contains audit sections and unchanged baseline bytes.
  - Existing `.entropy-baseline/latest.json` + hard-gate JSON command:
    `PYTHONDONTWRITEBYTECODE=1 uv run --no-sync python
    scripts/governance/audit_repo_entropy.py --mode hard-gate --format json`
    -> stdout parseable as JSON, hard-gate metadata present, and unchanged
    baseline bytes even when the command returns non-zero.
  - Focused automated test: `uv run --no-sync pytest -q
    tests/test_entropy_audit_script.py`.

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
- Fixture evidence:
  - No existing latest + explicit writer command:
    `PYTHONDONTWRITEBYTECODE=1 uv run --no-sync python
    scripts/governance/write_entropy_baseline.py --repo-root <tmp-repo>` ->
    creates `<tmp-repo>/.entropy-baseline/latest.json`, exits zero, and writes
    required comparison fields: branch, commit, summary metrics, modules,
    high-spread patterns, and cleanup priorities.
  - Existing latest + explicit writer command:
    `PYTHONDONTWRITEBYTECODE=1 uv run --no-sync python
    scripts/governance/write_entropy_baseline.py --repo-root <tmp-repo>` ->
    preserves the previous latest bytes under exactly one timestamped archive
    file before replacing latest.
  - Existing latest + normal report commands:
    `audit_repo_entropy.py --format json`, `--format markdown`, and `--mode
    hard-gate --format json` -> no baseline mutation, preserving the G6-01
    report-only tests.
  - Resource/write-surface bound:
    temp repo + explicit writer command -> writer obtains one audit snapshot and
    only creates or replaces `.entropy-baseline/latest.json` plus at most one
    timestamped archive; no files outside `.entropy-baseline/` are created by
    the writer.
  - Failure path: blocked archive or write path -> writer exits non-zero with a
    stable error and does not silently delete the previous latest baseline.
  - Focused automated test: `uv run --no-sync pytest -q
    tests/test_entropy_audit_script.py`.
  - Static check: `uv run --no-sync ruff check
    scripts/governance/audit_repo_entropy.py
    scripts/governance/write_entropy_baseline.py
    tests/test_entropy_audit_script.py`.

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
- Fixture evidence:
  - Current runbook route-authority grep:
    `rg -n '/hydro-met|/overview|/forecast|/meteorology|/flood-alerts|/basins/:id|/segments/:id|/basins/|/segments/' docs/runbooks/two-node-production-e2e-plan.md docs/runbooks/two-node-deployment-overview.md docs/runbooks/node-27-bringup-checklist.md`
    -> every hit is redirect, compatibility, or historical context; none is an
    active current live-proof instruction.
  - Current live proof wording check:
    `rg -n 'live browser|browser proof|浏览器|/ops|single-map|单页|重定向|redirect' docs/runbooks/two-node-production-e2e-plan.md docs/runbooks/two-node-deployment-overview.md docs/runbooks/node-27-bringup-checklist.md`
    -> current browser proof uses `/` plus `/ops`, with `/hydro-met -> /` only
    as legacy redirect smoke where retained.
  - Historical banner check:
    `rg -n 'historical|superseded|历史|已被|M26|single-map|单页' docs/runbooks/qhh-mvp-production-like-e2e-checklist.md docs/runbooks/qhh-mvp-smoke-evidence.md`
    -> old MVP `/hydro-met` evidence is visibly marked historical/superseded
    in every edited historical runbook and points to current M26 route
    authority.
  - OpenSpec validation:
    `openspec validate governance-6-entropy-structural-burndown --strict --no-interactive`
    -> valid.

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
- Acceptance: check covers every current legacy redirect alias from
  `DOC_STATUS.md`, including `/overview`, and fails on active current-runbook
  usage outside the allowlist classes.
- Fixture evidence:
  - Route-authority drift test:
    `uv run --no-sync pytest -q tests/test_entropy_audit_script.py -k route_authority`
    with input `docs/runbooks/current.md: "Open /forecast for current live
    browser proof."` -> exactly one route finding for `/forecast` with
    `check_id=stale-display-route-token`, `allowlist_state=unallowlisted`,
    `allowlist_key=null`, `budget_counted=true`, and `gate_eligible=false`.
  - Route-authority allowlist test:
    `uv run --no-sync pytest -q tests/test_entropy_audit_script.py -k route_authority`
    with inputs `docs/runbooks/current.md: "/hydro-met -> / redirect alias"`,
    `docs/runbooks/current.md: "Compatibility context keeps /meteorology deep
    links"`, and `docs/runbooks/current.md: "Historical pre-M26 evidence used
    /flood-alerts"` -> allowlisted findings with distinct
    `allowlist_reason`/`allowlist_key` values for redirect, compatibility, and
    historical classes.
  - Legacy alias coverage test:
    `uv run --no-sync pytest -q tests/test_entropy_audit_script.py -k route_authority`
    with input lines containing `/overview`, `/hydro-met`, `/forecast`,
    `/meteorology`, `/flood-alerts`, `/basins/:id`, `/segments/:id`,
    `/basins/demo`, and `/segments/demo` -> every token is represented in
    `stale-display-route-token` descriptions or evidence lines.
  - Resource-discovery bound test:
    `uv run --no-sync pytest -q tests/test_entropy_audit_script.py -k route_authority`
    with input `artifacts/generated.md: "Open /overview"` -> no
    `stale-display-route-token` finding for skipped artifact roots.
  - Full focused audit test slice:
    `uv run --no-sync pytest -q tests/test_entropy_audit_script.py` -> pass.
  - Static check:
    `uv run --no-sync ruff check scripts/governance/audit_repo_entropy.py tests/test_entropy_audit_script.py`
    -> pass.
  - OpenSpec validation:
    `openspec validate governance-6-entropy-structural-burndown --strict --no-interactive`
    -> valid.

### G6-05 Frontend mocked/live spec classification

- Implementation Ready: yes.
- Ownership: `apps/frontend/e2e/m11-routes.mocked.spec.ts`,
  `apps/frontend/e2e/monitoring.mocked.spec.ts`, frontend Playwright configuration
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
- Invariant: broad `page.route('**/api/v1/**')` API mocks are allowed only in
  mocked-regression, preview, or visual-classified specs. The `m11-routes` and
  `monitoring` mocked operator/retry/cancel checks must never be accepted as
  `display_readonly` live display receipts.
- Fixture evidence:
  - Run the affected mocked frontend specs after classification, for example
    `cd apps/frontend && corepack pnpm exec playwright test e2e/m11-routes.mocked.spec.ts e2e/monitoring.mocked.spec.ts`
    or the equivalent configured mocked-regression project command if the files
    move to a mocked-labelled directory; expected result: affected mocked specs
    pass under the mocked-regression project and are not presented as live
    receipts.
  - Run the existing frontend unit tests or the smallest affected subset that
    exercises Playwright config helper behavior.
  - Confirm live-display missing-env rejection with
    `cd apps/frontend && corepack pnpm run test:e2e:live-display` in an
    environment without `PLAYWRIGHT_LIVE_BASE_URL` and
    `PLAYWRIGHT_LIVE_API_BASE_URL`; expected result is a deterministic blocked
    profile error, not fallback to local dev or `https://api.example.test`.
  - Confirm live-display broad-mock rejection by running the helper/config test
    `cd apps/frontend && corepack pnpm test -- src/__tests__/playwrightConfig.test.ts`;
    expected result: the slice passes, including the case that
    `assertLiveDisplaySpecsDoNotMockApis` deterministically rejects a
    live-labelled broad API mock fixture.
  - Run a read-only audit confirmation, for example
    `uv run --no-sync pytest -q tests/test_entropy_audit_script.py -k broad_e2e_mock`,
    proving broad e2e API mock detector classification semantics for
    mocked, preview, and visual broad mocks under current audit semantics.

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
- Invariant: multiline `page.route(` registrations with `'**/api/v1/**'` are
  classified the same way as single-line broad mocks, with mocked/preview/visual
  paths allowlisted and live-labelled or otherwise unallowlisted paths
  gate-eligible.
- Fixture evidence:
  - Collect the detector slice explicitly with
    `PYTHONDONTWRITEBYTECODE=1 uv run --no-sync pytest --collect-only -q tests/test_entropy_audit_script.py -k broad_e2e_mock`;
    expected result: the five `test_broad_e2e_mock_*` detector tests are the
    collected tests.
  - Run the detector slice with
    `PYTHONDONTWRITEBYTECODE=1 uv run --no-sync pytest -q tests/test_entropy_audit_script.py -k broad_e2e_mock`;
    expected result: inline and multiline broad-mock fixtures behave as
    expected across live-labelled, mocked-labelled, preview, and visual paths.
  - Keep the current broad-mock parser regex and the `test_broad_e2e_mock_*`
    coverage aligned so the live-looking multiline scenario is not missed.

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
- Fixture level: expanded.
- Repair intensity: low.
- Mandatory expanded triggers: legacy/example evidence compatibility and
  published display identity wording; the docs define which frontend evidence
  lanes may be cited as live `display_readonly` receipts.
- Change surface: `docs/VALIDATION.md` frontend E2E validation guidance only.
- Must preserve:
  - Existing validation commands and live-display URL requirements.
  - Existing frontend specs, Playwright config, and entropy audit parser
    behavior from G6-05/G6-06.
- Must add/change:
  - `docs/VALIDATION.md` names the four frontend evidence lanes:
    mocked-regression, preview, visual, and live-display.
  - Each lane states whether broad API mocks such as
    `page.route('**/api/v1/**')` are allowed.
  - Each lane states whether its receipts may be cited as live
    `display_readonly` evidence.
  - Live-display guidance states broad API mocks cannot produce live display
    receipts and that live evidence requires explicit runtime frontend/API
    bindings.
- Risk packs considered:
  - Public API / CLI / script entry: not selected - docs-only command wording;
    no executable entrypoint changes.
  - Config / project setup: not selected - frontend configuration is out of
    scope.
  - File IO / path safety / overwrite: not selected - no file IO behavior
    changes.
  - Schema / columns / units / field names: not selected - no schema or
    evidence JSON contract changes.
  - Auth / permissions / secrets: not selected - docs reiterate existing
    `display_readonly` evidence boundary but do not change auth handling.
  - Concurrency / shared state / ordering: not selected - no runtime ordering
    changes.
  - Resource limits / large input / discovery: not selected - no discovery or
    parser changes.
  - Legacy compatibility / examples: selected - current mocked specs remain
    valid deterministic regression evidence but not live proof.
  - Error handling / rollback / partial outputs: not selected - no executable
    failure-path changes.
  - Release / packaging / dependency compatibility: not selected - no package
    or dependency changes.
  - Documentation / migration notes: selected - this issue is the validation
    documentation boundary.
- Domain packs considered:
  - Published NHMS artifacts / display identity: selected - display receipts
    must be bound to the live display profile, not mocked/preview/visual lanes.
  - Run manifest / QC provenance: not selected - no manifest/QC provenance
    change.
  - Slurm production lifecycle / mock-vs-real parity: not selected - frontend
    evidence lane wording only; no Slurm behavior.
  - Other NHMS domain packs: not selected - no geospatial, forcing, numerical,
    PostGIS, or provider behavior changes.
- Required evidence:
  - `rg -n "mocked-regression|preview|visual|live-display|page.route|display_readonly|live receipt|live display" docs/VALIDATION.md`
    -> the frontend E2E section names all four lanes, states broad mocks are
    allowed only for mocked-regression/preview/visual evidence, and states those
    lanes cannot produce live `display_readonly` receipts.
  - `openspec validate governance-6-entropy-structural-burndown --strict --no-interactive`
    -> valid.
- Non-goals:
  - No frontend spec/config changes.
  - No audit parser/test changes.
  - No change to live-display command semantics or URL requirements.

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
- Fixture level: compact.
- Repair intensity: low.
- Change surface: `docs/governance/DOC_STATUS.md` artifact ownership policy
  wording only.
- Must preserve:
  - Existing ownership distinctions for tracked `.agents/skills/**`, local
    `.codex/**` evidence, frontend visual artifacts, and root `artifacts/`.
  - Existing `.gitignore` and `.dockerignore` contents and audit parser
    behavior.
- Downstream compatibility axes: entropy audit consumers and Docker
  build-context policy readers continue to see the same ownership model, with
  the missing literal term added.
- Must add/change:
  - `DOC_STATUS.md` explicitly names the literal `.dockerignore` term in the
    artifact ownership policy so the audit-required ownership term is present.
  - The wording keeps Docker build context exclusion as policy context, not a
    new runtime or ignore-file behavior.
- Risk packs considered:
  - Config / project setup: selected - the wording documents Docker build
    context ownership expectations without changing config files.
  - Documentation / migration notes: selected - this issue is a governance doc
    source-of-truth correction.
  - Legacy compatibility / examples: selected - current tracked/generated
    ownership distinctions must remain intact.
  - Public API / CLI / script entry, File IO / path safety / overwrite, Schema /
    columns / units / field names, Auth / permissions / secrets, Concurrency /
    shared state / ordering, Resource limits / large input / discovery, Error
    handling / rollback / partial outputs, Release / packaging / dependency
    compatibility: not selected - no executable behavior, schema, auth,
    runtime, packaging, or parser changes.
- Domain packs considered: not selected - no NHMS geospatial, forcing,
  numerical, PostGIS, Slurm, provider, manifest/QC, or display artifact
  identity behavior changes.
- Required evidence:
  - `rg -n "\\.dockerignore|Agent And Artifact Ownership|Docker build context" docs/governance/DOC_STATUS.md`
    -> ownership policy explicitly mentions `.dockerignore`.
  - `PYTHONDONTWRITEBYTECODE=1 uv run --no-sync python scripts/governance/audit_repo_entropy.py --format json`
    followed by
    `jq -e '[.findings[] | select(.check_id == "agent-artifact-ownership-policy")] | length == 0'`
    -> current findings contain no `agent-artifact-ownership-policy` entry
    (the check family may still appear under metadata as an executed check).
  - `openspec validate governance-6-entropy-structural-burndown --strict --no-interactive`
    -> valid.
- Non-goals:
  - No changes to `.gitignore`, `.dockerignore`, audit parser code, audit
    tests, or broader governance doc structure.

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
- Fixture level: expanded.
- Repair intensity: high.
- Mandatory expanded triggers: shared orchestrator entrypoint, file IO/path
  safety, lock publish/delete/overwrite behavior, concurrency, persisted shared
  state, and mutation fences.
- Change surface:
  - `services/orchestrator/scheduler_lease.py` new lease module.
  - `services/orchestrator/scheduler.py` compatibility imports/re-exports and
    `_build_scheduler_lease` call site only.
  - `tests/test_production_scheduler.py` focused import/monkeypatch updates only
    if compatibility shims cannot keep existing tests unchanged.
- Must preserve:
  - File lock payload keys and values: `owner`, `schema_version`, `pass_id`,
    `lease_token`, `pid`, `host`, `heartbeat_seq`, `heartbeat_at`,
    `started_at`, and `lock_path`.
  - `FileSchedulerLease`, `PostgresSchedulerLease`, `_LeaseHeartbeat`,
    `_default_owner_liveness_probe`, `UnsafeSchedulerLockError`,
    `_open_lock_parent_directory`, `_open_regular_guard_file`, and
    `_unlink_lock_file` import/monkeypatch compatibility from
    `services.orchestrator.scheduler`.
  - CAS renew, crash-atomic renew temp/write/rename behavior, release CAS
    no-op on stolen token, live same-host holder non-reclaim, dead-holder
    reclaim, cross-host 2x TTL grace, CAS abort on concurrent renew, heartbeat
    lost detection, and lease-lost pre-mutation fence.
  - `PostgresSchedulerLease` advisory-lock evidence fields and file-guard
    bypass behavior.
- Must add/change:
  - Move lease classes/helpers into `scheduler_lease.py` without changing
    public constructor signatures, return payload shapes, exception reasons, or
    test monkeypatch paths from `scheduler.py`.
  - Keep `ProductionScheduler._build_scheduler_lease` behavior equivalent for
    file and postgres backends.
- Risk packs considered:
  - Public API / CLI / script entry: selected - scheduler imports and
    monkeypatch paths are compatibility surfaces.
  - Config / project setup: selected - `scheduler_lock_backend`, lock paths,
    ttl, database URL, and workspace root flow into lease construction.
  - File IO / path safety / overwrite: selected - lock parent traversal,
    symlink/non-regular/oversized lock rejection, guarded flock, atomic renew,
    unlink, and temp cleanup are lease responsibilities.
  - Schema / columns / units / field names: selected - lock and pass evidence
    payload field names must remain stable.
  - Auth / permissions / secrets: not selected - no credential handling change;
    database URL is passed through unchanged and must not be logged.
  - Concurrency / shared state / ordering: selected - CAS, heartbeat, reclaim,
    release, and lease-lost fences guard shared scheduler mutation.
  - Resource limits / large input / discovery: selected - bounded lock payload
    reads via `MAX_LOCK_PAYLOAD_BYTES` remain enforced.
  - Legacy compatibility / examples: selected - old imports from
    `scheduler.py` and existing tests/scripts such as
    `scripts/m24_lease_nfs_proof.py` must keep working.
  - Error handling / rollback / partial outputs: selected - unsafe lock errors,
    temp cleanup, failed renew, and contended evidence payloads remain stable.
  - Release / packaging / dependency compatibility: selected - new module must
    import without adding dependencies or circular imports.
  - Documentation / migration notes: not selected - no user-facing docs change
    required for this internal extraction.
- Domain packs considered:
  - Slurm production lifecycle / mock-vs-real parity: selected - lease-lost
    must still block orchestrator construction and Slurm submission.
  - Run manifest / QC provenance: not selected - no run manifest or QC change.
  - Published NHMS artifacts / display identity: not selected - no publish or
    display artifact behavior change.
  - Other NHMS domain packs: not selected - no geospatial, forcing, numerical,
    PostGIS, or provider behavior changes.
- Invariant Matrix:
  - Governing invariant: extracting lease code must not let a scheduler pass
    mutate or submit unless it still owns the exact active lease token and pass
    identity.
  - Source-of-truth identity/contract: lock file/advisory lock identity
    (`pass_id`, `lease_token`, `owner`, `schema_version`, `heartbeat_seq`,
    holder `pid`/`host`, `lock_path`) and `heartbeat.lost` fence state.
  - Surfaces:
    - Producers: `FileSchedulerLease.acquire`, `PostgresSchedulerLease.acquire`,
      `_LeaseHeartbeat`, and `ProductionScheduler._build_scheduler_lease`.
    - Validators/preflight: `_open_lock_parent_directory`,
      `_open_regular_guard_file`, `_existing_lock_state`,
      `_read_existing_lock`, `_default_owner_liveness_probe`.
    - Storage/cache/query: lock file plus guard file under `lock_path.parent`,
      renew temp file, and PostgreSQL advisory lock connection.
    - Public routes/entrypoints: `ProductionScheduler.run_once`,
      `ProductionSchedulerConfig`, and imports from
      `services.orchestrator.scheduler`.
    - Frontend/downstream consumers: none - scheduler backend-only extraction.
    - Failure paths/rollback/stale state: renew write failure cleanup, stolen
      token release no-op, unsafe parent/guard/symlink/non-regular/oversized
      lock handling, stale/dead/cross-host reclaim decisions.
    - Evidence/audit/readiness: `lock_result` payloads, `lease_lost` pass
      evidence, `no_mutation_proof`, and focused scheduler tests.
  - Regression rows:
    - Current holder renews with matching `pass_id`/`lease_token` -> heartbeat
      sequence increments, identity fields remain stable, lock JSON remains
      valid and non-empty.
    - Lock token is stolen or heartbeat marks lost before execution -> renewal
      returns false, `run_once` returns `lease_lost`, orchestrator factory is not
      called, and `slurm_submit_called` remains false.
    - Same-host live holder with aged mtime -> contender does not reclaim; dead
      holder -> contender reclaims; cross-host unknown holder -> only reclaims
      after 2x TTL.
    - Concurrent renew between stale decision and unlink -> contender aborts
      reclaim and preserves the renewed holder lock.
    - Unsafe lock parent/symlink/non-regular/oversized lock -> stable unsafe
      reason and no outside/partial file mutation.
    - Existing imports/monkeypatches from `services.orchestrator.scheduler`
      -> still resolve to the moved lease implementation.
- Boundary-surface checklist:
  - Shared helper roots: `scheduler.py` lease helpers being moved to
    `scheduler_lease.py`.
  - Public entrypoints: `ProductionScheduler.run_once`,
    `ProductionScheduler._build_scheduler_lease`, scheduler CLI plan/submit
    paths that instantiate scheduler.
  - Read surfaces: lock file reads, lock parent stat/open, guard file stat/open,
    PostgreSQL advisory lock connection outcome.
  - Write/delete/overwrite surfaces: lock acquire create, stale unlink, renew
    temp write/rename, release unlink, guard file creation.
  - Staging/publish/rollback surfaces: renew temp cleanup and failed write
    rollback.
  - Producer/consumer evidence boundaries: `lock_result`, `lease_lost` evidence,
    `no_mutation_proof`, postgres advisory lock evidence.
  - Stale-state/idempotency boundaries: same-host live holder, dead holder,
    cross-host TTL, concurrent renew CAS, stolen token release no-op.
  - Unchanged downstream consumers: `tests/test_production_scheduler.py`,
    `scripts/m24_lease_nfs_proof.py`, and any imports from
    `services.orchestrator.scheduler`.
- Required evidence:
  - `PYTHONDONTWRITEBYTECODE=1 uv run --no-sync pytest -q tests/test_production_scheduler.py -k 'lock or lease or heartbeat or postgres_lock_backend'`
    -> focused lease/import/mutation-fence tests pass.
  - `PYTHONDONTWRITEBYTECODE=1 uv run --no-sync pytest -q tests/test_production_scheduler.py`
    -> full production scheduler tests pass.
  - `PYTHONDONTWRITEBYTECODE=1 uv run --no-sync ruff check services/orchestrator tests/test_production_scheduler.py`
    -> lint passes.
  - `openspec validate governance-6-entropy-structural-burndown --strict --no-interactive`
    -> valid.
- Non-goals:
  - No candidate-state, discovery, candidate construction, execution, evidence,
    chain, reservation, retry, or Slurm protocol rewrites.
  - No status/reason/evidence key rename and no change to `.entropy-baseline`.

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
  `tests/test_production_scheduler.py`, `tests/test_retry.py`,
  `tests/test_retry_cancel_consistency.py`.
- Verification: `uv run --no-sync pytest -q tests/test_retry.py tests/test_retry_cancel_consistency.py`
  plus `uv run --no-sync ruff check services/orchestrator tests/test_retry.py tests/test_retry_cancel_consistency.py`.
- Acceptance: evidence keys, status/reason codes, schema versions, and old
  aliases remain unchanged; focused candidate-state and retry tests pass.
- Fixture level: expanded.
- Repair intensity: high.
- Mandatory expanded triggers: shared scheduler state machine, persisted
  pipeline/hydro state interpretation, retry/cancellation/manual retry,
  legacy compatibility aliases, bounded evidence ingestion, and Slurm
  lifecycle parity.
- Change surface:
  - `services/orchestrator/scheduler_state.py` new candidate-state module.
  - `services/orchestrator/scheduler.py` compatibility imports/re-exports and
    call-site wiring only.
  - `tests/test_production_scheduler.py`, `tests/test_retry.py`, and
    `tests/test_retry_cancel_consistency.py` import/compatibility coverage only
    where existing shims cannot keep tests unchanged.
- Must preserve:
  - `CandidateStateDecision` constructor/signature and action/reason/evidence
    semantics.
  - Candidate-state decisions and reason codes:
    `production_identity_mismatch`, `active_slurm_job`,
    `active_duplicate_pipeline`, `terminal_hydro_success`,
    `terminal_pipeline_success`, `manual_retry_requested`,
    `resume_downstream_after_durable_shud`, `repair_missing_raw_manifest`,
    `retry_downstream_after_raw_repair`, `permanent_failure_guard`,
    `retry_limit_exhausted`, `policy_blocked`,
    `manual_retry_required_after_cancelled`, and `retry_failed_candidate`.
  - Evidence keys and nested shapes including `candidate_identity`,
    `production_identity_validation`, `pipeline_jobs`, `pipeline_events`,
    `hydro_run`, `forcing_version`, `forecast_cycle`, `manual_retry`,
    `retry`, `state_bounds`, `decision`, `reason`, `retry_policy`,
    `failure`, `identity`, `active_slurm_jobs`, `replacement_submitted`,
    `restart_stage`, `restart_from_stage`, `fresh_ingestion`,
    `raw_manifest_repair`, and `manual_retry_required`.
  - Identity validation schema version
    `nhms.production.identity_validation.v1`, comparison aliases,
    `legacy_non_authoritative`, mismatch payloads, candidate-scoped shared
    cycle filtering, top-level legacy blocker filtering, and nested task-result
    bounds/overflow evidence.
  - Manual retry marker ordering, stale marker handling, active truth
    non-override, repaired historical failure handling, retry attempt/new
    attempt arithmetic, and prior failure reason propagation.
  - Secret redaction through moved candidate-state evidence paths, including
    `log_uri`, `error_message`, provider payloads, object-store evidence, and
    active Slurm/cancel evidence that currently rely on `_evidence_safe` /
    `redact_payload`.
  - Active Slurm skip behavior, terminal hydro/pipeline success precedence,
    cancelled/permanent failure blocking, downstream retry/restart stage
    selection, and raw-manifest repair decisions.
  - Private helper import/monkeypatch compatibility from
    `services.orchestrator.scheduler` for moved candidate-state helpers until
    downstream callers/tests migrate.
- Must add/change:
  - Move candidate-state dataclass/helpers into `scheduler_state.py` without
    changing public scheduler candidate evidence, skipped/blocked/candidate
    payloads, retry semantics, or old private helper call paths.
  - Keep `scheduler.py` as the compatibility surface for old imports and
    monkeypatches, including helpers used directly by current tests.
  - Keep state-provider and active-Slurm-provider fallback signatures stable
    for old providers that do not accept newer keyword arguments.
- Risk packs considered:
  - Public API / CLI / script entry: selected - scheduler private helpers,
    tests, scripts, and pass evidence are compatibility surfaces.
  - Config / project setup: selected - retry limits, candidate-state row/event
    limits, object-store roots used by raw manifest repair, and scheduler
    provider signatures flow into decisions.
  - File IO / path safety / overwrite: selected - raw manifest existence checks
    use `LocalObjectStore` and object-store roots; extraction must not widen
    path semantics.
  - Schema / columns / units / field names: selected - evidence keys, schema
    version, status/reason codes, retry fields, and identity aliases must stay
    byte-shape compatible.
  - Auth / permissions / secrets: selected - moved candidate-state evidence
    may carry secret-bearing `log_uri`, `error_message`, provider payload, or
    object-store values; redaction behavior must remain stable even though no
    auth boundary changes.
  - Concurrency / shared state / ordering: selected - active Slurm, active
    pipeline, manual retry, terminal truth, repaired-stage truth, and
    cancellation ordering decide whether submission is allowed.
  - Resource limits / large input / discovery: selected - candidate-state job,
    event, and task-result bounds prevent evidence amplification.
  - Legacy compatibility / examples: selected - legacy non-authoritative rows,
    old identity aliases, old provider signatures, and old scheduler private
    helper imports remain supported.
  - Error handling / rollback / partial outputs: selected - blocked/retry/skip
    decisions must keep stable failure evidence and no replacement submission
    when not allowed.
  - Release / packaging / dependency compatibility: selected - new module must
    import without new dependencies or circular imports after the lease split.
  - Documentation / migration notes: not selected - internal extraction only;
    OpenSpec/PR evidence is sufficient migration record.
  - Slurm production lifecycle / mock-vs-real parity: selected - active Slurm
    duplicate skip, cancel/manual retry, and submitted/replacement evidence
    must remain stable.
  - Run manifest / QC provenance: selected - state evidence is copied into
    candidate/model-run/submission manifests and must preserve identity fields.
  - Published NHMS artifacts / display identity: selected - published manifest
    identity participates in production identity validation.
  - Other NHMS domain packs: not selected - no geospatial, forcing-window,
    numerical, PostGIS schema, or provider discovery behavior changes.
- Invariant Matrix:
  - Governing invariant: extracting candidate-state code must not let a
    candidate submit, retry, skip, or block under a different state truth than
    the current scheduler would derive from the same persisted state and active
    Slurm inputs.
  - Source-of-truth identity/contract: candidate production identity
    (`candidate_id`, `run_id`, `source_id`, `cycle_time`, `model_id`,
    `basin_id`, `basin_version_id`, `river_network_version_id`,
    `canonical_product_id`, `forcing_version_id`, `hydro_run_id`,
    `published_manifest_id`), pipeline/hydro statuses, retry/manual retry
    markers, active Slurm job ids, and evidence schema
    `nhms.production.identity_validation.v1`.
  - Surfaces:
    - Producers: candidate-state provider payloads, active Slurm provider
      payloads, `CandidateStateDecision`, `_candidate_state_decision`,
      `_candidate_state_evidence`, `_candidate_state_identity_validation`.
    - Validators/preflight: bounded state/event/task sampling, production
      identity validation, legacy/non-authoritative filtering, provider
      signature fallback, and raw manifest existence checks.
    - Storage/cache/query: persisted pipeline jobs/events, hydro run state,
      forcing/canonical/published identity rows, object-store raw manifest
      existence checks, and in-memory active Slurm query results.
    - Public routes/entrypoints: `ProductionScheduler._build_candidates`,
      `ProductionScheduler.run_once`, retry/cancel API behavior exercised by
      `tests/test_retry.py` and `tests/test_retry_cancel_consistency.py`, and
      imports from `services.orchestrator.scheduler`.
    - Frontend/downstream consumers: model-run evidence, submitted basin
      manifest state evidence, scheduler pass evidence, and downstream retry
      API responses.
    - Failure paths/rollback/stale state: identity mismatch, active duplicate,
      terminal success, stale/active manual retry markers, repaired-stage
      history, permanent/cancelled states, bounded overflow, raw manifest
      repair, and provider `TypeError` fallback.
    - Evidence/audit/readiness: skipped/blocked/candidate evidence, retry
      policy evidence, manual retry evidence, state bounds, and focused
      candidate-state/retry tests.
  - Regression rows:
    - Matching current candidate state with active Slurm job -> skip with
      `active_slurm_job`, preserve `active_slurm_jobs`, and no replacement
      submission.
    - Candidate-state identity mismatch in any authoritative row -> block with
      `production_identity_mismatch`, preserve validation mismatch payload and
      do not submit.
    - Legacy/non-authoritative rows without M23 proof -> do not drive retry,
      block, cancel, or terminal decisions; old compatible proof may still skip
      terminal same-candidate success.
    - Manual retry marker newer than terminal/permanent failure -> candidate is
      allowed with stable `manual_retry` attempt/prior-failure evidence; stale
      or active-blocked marker does not override active truth.
    - Terminal hydro/pipeline success newer than failed evidence -> skip
      terminal and reuse durable evidence; manual retry marker does not
      override newer terminal truth.
    - Permanent, exhausted, or cancelled candidate state -> block with stable
      retry policy/manual retry required evidence until explicit manual retry.
    - Bounded job/event/task-result inputs over limits -> evidence includes
      bounds/overflow metadata and out-of-bound rows do not drive decisions.
    - Secret-bearing URLs/messages in candidate-state, active Slurm, cancel,
      or retry evidence -> credentials remain redacted in skipped/blocked/API
      evidence after extraction.
    - Existing imports/monkeypatches from `services.orchestrator.scheduler`
      for moved candidate-state helpers -> still resolve to moved
      implementation.
- Boundary-surface checklist:
  - Shared helper roots: scheduler candidate-state helpers moved to
    `scheduler_state.py`.
  - Public entrypoints: `ProductionScheduler._build_candidates`,
    `ProductionScheduler.run_once`, retry/cancel API tests that depend on
    candidate-state evidence.
  - Read surfaces: candidate-state provider payload, active Slurm provider
    payload, pipeline jobs/events, hydro status, identity containers, raw
    manifest object existence.
  - Write/delete/overwrite surfaces: none introduced; decisions only permit or
    prevent downstream submission/mutation.
  - Staging/publish/rollback surfaces: raw manifest repair evidence and
    downstream restart-stage selection only, no publish behavior change.
  - Producer/consumer evidence boundaries: candidate evidence, blocked/skipped
    evidence, model-run evidence, basin manifest state evidence, retry API
    error/response evidence.
  - Stale-state/idempotency boundaries: active Slurm duplicate skip, active
    pipeline duplicate skip, stale manual retry markers, repaired historical
    failures, terminal truth precedence.
  - Unchanged downstream consumers: discovery, candidate construction,
    execution, evidence assembly, chain stage execution, reservation/reconcile,
    and Slurm protocol bodies.
- Required evidence:
  - `PYTHONDONTWRITEBYTECODE=1 uv run --no-sync pytest -q tests/test_production_scheduler.py -k 'candidate_state or manual_retry or active_slurm or terminal_hydro or terminal_pipeline or production_identity_mismatch'`
    -> focused candidate-state, identity, active Slurm, manual retry, and
    terminal truth tests pass.
  - `PYTHONDONTWRITEBYTECODE=1 uv run --no-sync pytest -q tests/test_production_scheduler.py -k 'redacts_secret_urls_and_error_messages or candidate_state'`
    -> candidate-state and active Slurm/cancel evidence redaction remains
    stable after helper extraction.
  - `PYTHONDONTWRITEBYTECODE=1 uv run --no-sync pytest -q tests/test_retry.py tests/test_retry_cancel_consistency.py`
    -> retry/manual retry/cancel compatibility tests pass.
  - `PYTHONDONTWRITEBYTECODE=1 uv run --no-sync pytest -q tests/test_production_scheduler.py`
    -> full production scheduler tests pass if focused changes touch shared
    candidate construction surfaces.
  - `PYTHONDONTWRITEBYTECODE=1 uv run --no-sync ruff check services/orchestrator tests/test_production_scheduler.py tests/test_retry.py tests/test_retry_cancel_consistency.py`
    -> lint passes.
  - `openspec validate governance-6-entropy-structural-burndown --strict --no-interactive`
    -> valid.
- Non-goals:
  - No discovery, candidate construction, execution, evidence module,
    reservation, reconcile, retry service, or chain stage behavior rewrite.
  - No status/reason/evidence key rename and no change to `.entropy-baseline`.
  - No retirement of scheduler private helper shims in this issue.

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
- Fixture level: high.
- Repair intensity: high.
- Change surface:
  - `ProductionScheduler._discover_cycles`,
    `ProductionScheduler._discover_source_window`, and
    `ProductionScheduler._cycle_completion_status`.
  - New `services/orchestrator/scheduler_discovery.py` extraction module.
  - Discovery/backfill evidence helpers needed to preserve selected,
    deferred, duplicate, and unavailable source-cycle evidence.
- Must preserve:
  - Backfill mode remains `bool(backfill_enabled and models)`.
  - Backfill selects the oldest available incomplete cycle first.
  - Later source-local and global gaps are deferred with the existing reason
    codes and evidence shapes.
  - Empty-model backfill falls back to legacy newest-cycle mode with no
    backfill audit/deferred evidence.
  - Discovery date-window scanning, duplicate filtering,
    `MAX_DISCOVERED_CYCLES`, source ordering, and adapter `TypeError`
    fallback remain unchanged.
- Risk packs considered:
  - Public API / CLI / script entry: selected - `run_once` and
    `plan-production` expose discovery/backfill pass evidence.
  - Config / project setup: selected - `lookback_hours`,
    `cycle_lag_hours`, `max_cycles_per_source`, source filters, and
    `backfill_enabled` drive selection.
  - File IO / path safety / overwrite: not selected - no file read/write
    surface moves in this issue.
  - Schema / columns / units / field names: selected - pass evidence keys,
    `backfill_audit`, `backfill_deferred`, and source-cycle evidence fields
    must remain stable.
  - Auth / permissions / secrets: selected - source discovery evidence and
    probe URIs must keep existing redaction behavior when moved.
  - Concurrency / shared state / ordering: selected - cycle ordering and
    warm-start gap sequencing are the core behavior.
  - Resource limits / large input / discovery: selected - daily discovery
    scanning and `MAX_DISCOVERED_CYCLES` limit are in scope.
  - Legacy compatibility / examples: selected - private method callers and
    tests import helpers from `scheduler.py`; empty-model legacy mode must
    remain.
  - Error handling / rollback / partial outputs: selected - adapter absence,
    unavailable cycles, provider fallbacks, and resource-limit failures must
    keep stable evidence/status behavior.
  - Release / packaging / dependency compatibility: selected - new module
    must not create circular imports and old scheduler imports/methods stay
    available.
  - Documentation / migration notes: selected - OpenSpec task and evidence
    state must align with the extraction.
- Domain risk packs considered:
  - Geospatial / CRS / basin geometry: not selected - discovery/backfill
    selection reads source cycle identity and model ids only; basin geometry,
    CRS, and raster/vector alignment remain candidate construction/runtime
    concerns.
  - Hydro-met time series / forcing windows: selected - lookback, cycle lag,
    source-cycle hour, GFS/IFS source windows, and horizon metadata determine
    which forecast cycles may enter candidate construction.
  - SHUD numerical runtime / conservation / NaN: not selected - no SHUD
    execution, restart, output cadence, or numerical result handling changes.
  - PostGIS / TimescaleDB domain behavior: not selected - this issue does
    not alter migrations, SQL, hypertables, geometry queries, or DB schema;
    repository providers are only read through existing interfaces.
  - Slurm production lifecycle / mock-vs-real parity: selected - completion
    checks use active/candidate state repositories and must preserve stale
    pipeline truth.
  - External hydro-met providers / snapshot reproducibility: selected -
    source adapters, probe status, rate-limit/probe-failure evidence, and
    source-cycle availability decide selection without consuming source
    budget for unavailable cycles.
  - Run manifest / QC provenance: not selected - candidate/run manifest
    assembly is out of scope for G6-11.
  - Published NHMS artifacts / display identity: not selected - publish and
    frontend display identity are out of scope.
- Invariant Matrix:
  - Governing invariant: discovery extraction must not change which
    source/cycle pairs enter candidate construction or which source-cycle and
    backfill evidence rows explain excluded/deferred cycles.
  - Source-of-truth identity/contract: normalized `source_id`,
    `cycle_id_for(source_id, cycle_time)`, UTC `cycle_time`,
    `cycle_hour`, source adapter horizon metadata, and model completion truth
    from `has_completed_pipeline` or candidate-state terminal decisions.
  - Surfaces:
    - Producers: source adapters returning `CycleDiscovery`,
      registry-selected models, completion provider,
      candidate-state provider, and scheduler config.
    - Validators/preflight: date-window boundaries,
      `MAX_DISCOVERED_CYCLES`, duplicate cycle filtering, source id/window
      filtering, source horizon metadata, and completion status checks.
    - Storage/cache/query: active repository `has_completed_pipeline` and
      `candidate_state`; no DB schema changes.
    - Public routes/entrypoints: `ProductionScheduler.run_once`,
      `ProductionScheduler._discover_cycles`,
      `ProductionScheduler._discover_source_window`,
      `ProductionScheduler._cycle_completion_status`, and CLI planning tests.
    - Frontend/downstream consumers: scheduler pass evidence consumed by ops
      runbooks and downstream candidate construction; no frontend code change.
    - Failure paths/rollback/stale state: missing adapter, unavailable source
      cycles, duplicate cycles, over-limit discovery, no completion provider,
      old adapter signature fallback, empty models, and stale/completed
      candidate-state truth.
    - Evidence/audit/readiness: `source_cycles`, `backfill.enabled`,
      `backfill.audit`, `backfill_deferred`, duplicate exclusions,
      unavailable not-selected evidence, probe URI redaction, and focused
      backfill tests.
  - Regression rows:
    - Backfill enabled with models and newest completed cycle plus older gap
      -> select the older gap, not the newest completed cycle.
    - Backfill enabled with multiple available gaps -> select only the oldest
      eligible gap and defer later gaps with
      `backfill_deferred_waiting_for_prior_cycle`.
    - Backfill enabled across multiple sources with later selected gaps ->
      keep only the global earliest selected cycle and defer later ones with
      `backfill_deferred_waiting_for_global_prior_cycle`.
    - Backfill enabled with empty models -> legacy newest-cycle mode, no
      `backfill_audit`, and no `backfill_deferred`.
    - No completion provider -> treat discovered cycles as gaps without
      raising and preserve audit counts.
    - Completion provider is false or absent but every selected model has
      candidate-state terminal `terminal_hydro_success` or
      `terminal_pipeline_success` -> `_cycle_completion_status` returns
      `complete`, the cycle does not consume the backfill execution slot, and
      `backfill_audit.complete_count`, `gap_count`, and `selected_count`
      reflect the skipped completed cycle.
    - Candidate-state completion fallback has one model with missing or
      non-terminal state -> `_cycle_completion_status` returns `gap`, the
      oldest gap is selected, and later gaps remain deferred.
    - Adapter returns duplicate or out-of-window cycles -> duplicate/outside
      rows do not enter candidate construction and duplicate evidence remains
      stable.
    - Discovery count exceeds `MAX_DISCOVERED_CYCLES` -> scheduler returns
      the existing limit evidence/status path.
    - Source discovery/probe evidence contains secret-bearing keys or URLs
      -> redacted evidence remains redacted after extraction.
- Boundary-surface checklist:
  - Shared helper roots: scheduler discovery/backfill helpers moved to
    `scheduler_discovery.py`.
  - Public entrypoints: `run_once`, `_discover_cycles`,
    `_cycle_completion_status`, `_discover_source_window`, and CLI
    `plan-production` lookback behavior.
  - Read surfaces: source adapters, model registry output, active repository
    completion/candidate-state providers, scheduler config.
  - Write/delete/overwrite surfaces: none introduced; discovery only chooses
    later candidate construction inputs and evidence.
  - Staging/publish/rollback surfaces: none; no artifact publish behavior.
  - Producer/consumer evidence boundaries: source-cycle evidence, backfill
    audit/deferred evidence, duplicate evidence, unavailable source evidence,
    pass-level `backfill` evidence.
  - Stale-state/idempotency boundaries: completed-cycle skip, terminal
    candidate-state completion fallback, old adapter signature fallback,
    duplicate cycle filtering, global prior-cycle defer.
  - Unchanged downstream consumers: candidate construction, execution,
    evidence serialization, scheduler lease/reconcile, chain behavior.
- Required evidence:
  - `PYTHONDONTWRITEBYTECODE=1 uv run --no-sync pytest -q tests/test_scheduler_backfill.py`
    -> focused backfill, discovery, and CLI lookback tests pass.
  - `PYTHONDONTWRITEBYTECODE=1 uv run --no-sync pytest -q tests/test_scheduler_backfill.py -k 'candidate_state_completion_fallback'`
    -> terminal candidate-state fallback marks completed cycles complete, and
    mixed/non-terminal model state remains a gap.
  - `PYTHONDONTWRITEBYTECODE=1 uv run --no-sync pytest -q tests/test_production_scheduler.py -k 'source_cycle_evidence or cycle_discovery_limit'`
    -> source-cycle evidence redaction and discovery resource-limit paths pass.
  - `PYTHONDONTWRITEBYTECODE=1 uv run --no-sync pytest -q tests/test_production_scheduler.py`
    -> full production scheduler tests pass if extraction touches shared
    scheduler import or candidate-state compatibility surfaces.
  - `PYTHONDONTWRITEBYTECODE=1 uv run --no-sync ruff check services/orchestrator tests/test_scheduler_backfill.py tests/test_production_scheduler.py`
    -> lint passes.
  - `openspec validate governance-6-entropy-structural-burndown --strict --no-interactive`
    -> valid.
- Implementation evidence (2026-06-15):
  - `PYTHONDONTWRITEBYTECODE=1 uv run --no-sync pytest -q tests/test_scheduler_backfill.py`
    -> 18 passed.
  - `PYTHONDONTWRITEBYTECODE=1 uv run --no-sync pytest -q tests/test_scheduler_backfill.py -k 'legacy_adapter or typeerror or out_of_window or wrong_source'`
    -> 2 passed, 16 deselected.
  - `PYTHONDONTWRITEBYTECODE=1 uv run --no-sync pytest -q tests/test_scheduler_backfill.py -k 'candidate_state_completion_fallback or monkeypatch'`
    -> 5 passed, 13 deselected.
  - `PYTHONDONTWRITEBYTECODE=1 uv run --no-sync pytest -q tests/test_production_scheduler.py -k 'source_cycle_evidence or cycle_discovery_limit'`
    -> 2 passed, 519 deselected.
  - `PYTHONDONTWRITEBYTECODE=1 uv run --no-sync pytest -q tests/test_production_scheduler.py`
    -> 521 passed.
  - `PYTHONDONTWRITEBYTECODE=1 uv run --no-sync ruff check services/orchestrator tests/test_scheduler_backfill.py tests/test_production_scheduler.py`
    -> All checks passed.
  - `openspec validate governance-6-entropy-structural-burndown --strict --no-interactive`
    -> Change is valid.
  - `git diff --check`
    -> passed.
- Acceptance: oldest-gap-first, later-gap defer, and empty-model legacy fallback
  behavior remain unchanged.
- Non-goals:
  - No candidate construction, execution, evidence serialization, lease,
    reservation, reconcile, retry service, chain stage, DB schema, or frontend
    behavior rewrite.
  - No status/reason/evidence key rename and no change to `.entropy-baseline`.
  - No retirement of scheduler private method compatibility in this issue.

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
- Required Reading:
  `openspec/changes/governance-6-entropy-structural-burndown/specs/orchestrator-structural-burndown/spec.md`,
  `tests/test_production_scheduler.py`.
- Verification: `uv run --no-sync pytest -q tests/test_production_scheduler.py`
  plus `uv run --no-sync ruff check services/orchestrator tests/test_production_scheduler.py`.
- Acceptance: focused canonical readiness, active Slurm sync, and candidate
  selection tests pass.
- Fixture level: high.
- Repair intensity: high.
- Change surface:
  - `ProductionScheduler._build_candidates` and helper logic that constructs,
    blocks, skips, or annotates `SchedulerCandidate` values before execution.
  - New `services/orchestrator/scheduler_candidates.py` extraction module.
  - Compatibility aliases/shims in `scheduler.py` for candidate construction
    helpers used by tests or downstream imports.
  - Candidate selection evidence in `run_once` that consumes
    `_build_candidates` return values.
- Must preserve:
  - `_build_candidates(models, cycles, allow_slurm_status_sync=False)`
    returns the same five-tuple order:
    `candidates`, `blocked`, `skipped`, `duplicate_exclusions`,
    `slurm_status_sync_evidence`.
  - `SchedulerCandidate` identity fields, `to_dict()` shape,
    `candidate_id`, `run_id`, `forcing_version_id`,
    `canonical_product_id`, and state-evidence merge behavior.
  - Candidate construction order: candidate-id duplicate exclusion before
    source availability checks; unavailable source cycles block before active
    repository checks; completed duplicate pipelines skip before canonical
    readiness queries; terminal/active candidate-state decisions short-circuit
    not-ready canonical gates.
  - Candidate-state decisions from `scheduler_state.py` keep their current
    precedence: identity mismatch blocks, non-active skip decisions skip,
    active Slurm decisions may defer/sync/cancel, retry decisions may attach
    restart evidence unless fresh full-chain logic suppresses it.
  - Canonical readiness behavior remains stable: provider absence/query
    failure blocks with unavailable evidence, not-ready canonical rows block,
    true zero-row canonical readiness is fresh full-chain and never inherits
    a stale `restart_stage`, and empty/no-expected-lead readiness does not
    become fresh ingestion.
  - Active Slurm sync behavior remains stable: when sync is not allowed it
    emits `active_slurm_status_sync_deferred`; when sync is allowed it calls
    `sync_cycle_statuses`, re-queries state/active jobs, attaches
    `slurm_state_sync`, handles terminal updates, failed retries, and
    unknown-after-attempt failures without duplicate submission.
  - Duplicate candidate identity evidence remains included in both
    `skipped_candidates` and `duplicate_exclusions`.
  - `MAX_CANDIDATES` resource-limit behavior and evidence stay unchanged.
- Risk packs considered:
  - Public API / CLI / script entry: selected - `run_once` pass evidence and
    `ProductionScheduler._build_candidates` are exercised by production
    scheduler tests and may be used by private callers.
  - Config / project setup: selected - `cancel_active_slurm`, `dry_run`,
    candidate-state limits, model/source filters, and canonical readiness
    provider availability change candidate decisions.
  - File IO / path safety / overwrite: not selected - candidate construction
    does not introduce file writes; pre-execution evidence and runtime-root
    writes remain execution/evidence concerns.
  - Schema / columns / units / field names: selected - candidate, skipped,
    blocked, duplicate, canonical readiness, and Slurm sync evidence keys
    must remain stable.
  - Auth / permissions / secrets: selected - canonical readiness query
    failures, active Slurm jobs, provider payloads, and evidence paths can
    carry secret-bearing fields that must remain redacted.
  - Concurrency / shared state / ordering: selected - active orchestration,
    active pipeline, active Slurm, sync/re-query ordering, and duplicate
    candidate identities decide whether submission is allowed.
  - Resource limits / large input / discovery: selected - `MAX_CANDIDATES`,
    bounded active Slurm jobs, and candidate-state job/event limits are
    relevant to construction.
  - Legacy compatibility / examples: selected - old private imports and
    `ProductionScheduler._build_candidates` monkeypatch/call paths must
    continue through shims during extraction.
  - Error handling / rollback / partial outputs: selected - Slurm sync
    failure and canonical provider failure must keep conservative evidence
    and no duplicate or unsafe replacement submission.
  - Release / packaging / dependency compatibility: selected - the new module
    must not introduce circular imports with `scheduler.py`,
    `scheduler_state.py`, or `scheduler_discovery.py`.
  - Documentation / migration notes: selected - OpenSpec tasks and PR
    evidence must identify the moved candidate-construction boundary.
- Domain risk packs considered:
  - Hydro-met time series / forcing windows: selected - candidate horizon and
    source readiness context are passed into canonical readiness and must be
    reused consistently across models for a source/cycle.
  - Slurm production lifecycle / mock-vs-real parity: selected - active Slurm
    duplicate skip, sync defer/sync/failure, cancellation-requested skip, and
    status re-query behavior are core candidate-construction outcomes.
  - External hydro-met providers / snapshot reproducibility: selected -
    source object identity and policy identity flow into canonical readiness
    and fresh full-chain decisions.
  - Run manifest / QC provenance: selected - candidate state evidence is later
    copied into submitted basin/model-run manifests; the constructed payload
    shape must stay stable.
  - Published NHMS artifacts / display identity: selected - canonical product
    identity participates in readiness and candidate provenance.
  - Geospatial / CRS / basin geometry: not selected - extraction only moves
    candidate construction and does not alter basin geometry validation.
  - SHUD numerical runtime / conservation / NaN: not selected - no SHUD
    execution or numerical result handling moves in G6-12.
  - PostGIS / TimescaleDB domain behavior: not selected - no migrations,
    schema, hypertables, or geometry queries change in this issue.
- Invariant Matrix:
  - Governing invariant: candidate construction extraction must not change
    which candidate is submitted, skipped, blocked, or marked duplicate for
    the same models, source cycles, canonical readiness, candidate state, and
    active Slurm inputs.
  - Source-of-truth identity/contract: `SchedulerSourceCycle.discovery`,
    source horizon metadata, `RegisteredSchedulerModel`, `SchedulerCandidate`
    identity fields, canonical readiness provider output, candidate-state
    decisions, active repository duplicate truth, active Slurm jobs, and
    `cycle_id_for(source_id, cycle_time)`.
  - Surfaces:
    - Producers: registry-selected models, discovery-selected source cycles,
      `_candidate_for`, canonical readiness provider, candidate-state
      provider, active repository duplicate providers, active Slurm provider,
      and scheduler config.
    - Validators/preflight: candidate-id dedupe, source availability,
      active orchestration/pipeline duplicate checks, completion checks,
      candidate-state identity/skip/block/retry decisions, canonical
      readiness gating, fresh-zero-row recognition, active Slurm sync
      re-query, and `MAX_CANDIDATES`.
    - Storage/cache/query: active repository methods
      `has_active_orchestration`, `has_active_pipeline`,
      `has_completed_pipeline`, `candidate_state`, `active_slurm_jobs`; no DB
      schema changes.
    - Public routes/entrypoints: `ProductionScheduler.run_once`,
      `ProductionScheduler._build_candidates`, and imports from
      `services.orchestrator.scheduler`.
    - Frontend/downstream consumers: scheduler pass evidence, submitted basin
      payloads, model-run evidence, retry/cancel API consumers, and chain
      execution inputs.
    - Failure paths/rollback/stale state: unavailable source cycle, duplicate
      candidate identity, completed duplicate, active duplicate, identity
      mismatch, terminal success, active Slurm defer/sync/failure, canonical
      provider absence/failure, not-ready canonical rows, fresh full-chain
      zero rows, and candidate limit overflow.
    - Evidence/audit/readiness: `candidates`, `blocked_candidates`,
      `skipped_candidates`, `duplicate_exclusions`,
      `slurm_status_sync_evidence`, `canonical_readiness`,
      `fresh_ingestion`, `active_slurm_jobs`, and state evidence nested in
      candidate dictionaries.
  - Regression rows:
    - Completed duplicate pipeline -> skip with
      `completed_duplicate_pipeline` before canonical readiness provider is
      queried.
    - Candidate-state terminal hydro/pipeline success -> skip terminal before
      not-ready canonical gate and do not attach canonical readiness evidence.
    - Candidate-state active Slurm job -> skip/defer/sync according to
      `allow_slurm_status_sync`, `dry_run`, and `cancel_active_slurm`; do not
      submit a duplicate replacement unless the existing behavior allows it.
    - Slurm sync succeeds with failed terminal update -> re-query state,
      attach `slurm_state_sync`, and allow retry candidate submission exactly
      once.
    - Slurm sync throws after attempt -> return
      `active_slurm_status_sync_failed` / unknown-after-attempt evidence and
      no orchestrator submission.
    - Canonical readiness provider absent or query error -> block with
      `canonical_unavailable` evidence and redacted error details.
    - Not-ready canonical rows with expected leads -> block with existing
      reason and no fresh ingestion marker.
    - Zero canonical rows with real expected leads -> mark
      `fresh_ingestion.required` and force full-chain candidate/cohort with no
      inherited `restart_stage`.
    - Empty/no-expected-lead canonical evaluation -> hard-block and do not
      classify as fresh full-chain.
    - Duplicate candidate identity -> exclude duplicate candidate and record
      candidate duplicate evidence without changing selected candidate order.
    - Two models/cycles that resolve to the same `candidate_id` inside
      `_build_candidates` -> first candidate remains selected, later
      duplicates are excluded with `duplicate_candidate_identity` evidence.
    - Source object identity and horizon readiness context -> reused across
      models for one scheduler pass without changing provider inputs.
    - Candidate count exceeds `MAX_CANDIDATES` -> resource-limit status and
      evidence remain stable before downstream submission/evidence mutation.
- Boundary-surface checklist:
  - Shared helper roots: scheduler candidate-construction helpers moved to
    `scheduler_candidates.py`.
  - Public entrypoints: `ProductionScheduler._build_candidates`,
    `ProductionScheduler.run_once`, and `services.orchestrator.scheduler`
    helper imports.
  - Read surfaces: selected models, selected source cycles, candidate-state
    payloads, active Slurm jobs, canonical readiness provider, scheduler
    config, active repository duplicate truth.
  - Write/delete/overwrite surfaces: none introduced; construction only
    chooses downstream submission inputs and evidence.
  - Staging/publish/rollback surfaces: submitted basin payloads and
    fresh/retry restart-stage evidence only; no publish behavior change.
  - Producer/consumer evidence boundaries: candidate dictionaries, blocked
    and skipped evidence, duplicate exclusions, Slurm sync evidence,
    canonical readiness evidence, model-run evidence consumers.
  - Stale-state/idempotency boundaries: active duplicate checks,
    completed duplicate checks, terminal state truth, active Slurm sync
    re-query, candidate-scoped retry exceptions, and fresh full-chain restart
    suppression.
  - Unchanged downstream consumers: discovery/backfill selection, execution,
    evidence serialization, scheduler lease/reconcile, retry service, chain
    stage execution, DB schema, and frontend.
- Required evidence:
  - `PYTHONDONTWRITEBYTECODE=1 uv run --no-sync pytest -q tests/test_production_scheduler.py -k 'fresh_cycle_with_zero_canonical or completed_duplicate_is_skipped_before_not_ready_canonical_gate or scheduler_caps_reject_oversized_config or duplicate_active_model_identity or duplicate_active_package_identity or stale_active_db_job_terminal_slurm_sync'`
    -> focused zero-canonical fresh full-chain, completed duplicate
    short-circuit, candidate resource limit, duplicate active model/package,
    and stale active Slurm terminal-sync tests pass.
  - Add and run a focused regression named with `duplicate_candidate_identity`
    that exercises the `_build_candidates` duplicate candidate-id branch and
    proves both skipped and duplicate-exclusion evidence are emitted.
  - `PYTHONDONTWRITEBYTECODE=1 uv run --no-sync pytest -q tests/test_production_scheduler.py -k 'sync_cycle_statuses or active_slurm_status_sync or cancel_active_slurm'`
    -> Slurm sync defer/sync/failure/cancel candidate behavior remains
    stable.
  - `PYTHONDONTWRITEBYTECODE=1 uv run --no-sync pytest -q tests/test_production_scheduler.py -k 'duplicate_candidate_identity'`
    -> duplicate candidate-id regression passes.
  - `PYTHONDONTWRITEBYTECODE=1 uv run --no-sync pytest -q tests/test_production_scheduler.py -k 'redacts_secret_urls_and_error_messages or canonical_readiness_query_error'`
    -> candidate-construction evidence redaction remains stable.
  - `PYTHONDONTWRITEBYTECODE=1 uv run --no-sync pytest -q tests/test_production_scheduler.py`
    -> full production scheduler tests pass because this extraction touches
    shared candidate construction and downstream submission inputs.
  - `PYTHONDONTWRITEBYTECODE=1 uv run --no-sync ruff check services/orchestrator tests/test_production_scheduler.py`
    -> lint passes.
  - `openspec validate governance-6-entropy-structural-burndown --strict --no-interactive`
    -> valid.
- Implementation evidence (2026-06-15):
  - `PYTHONDONTWRITEBYTECODE=1 uv run --no-sync pytest -q tests/test_production_scheduler.py -k 'fresh_cycle_with_zero_canonical or completed_duplicate_is_skipped_before_not_ready_canonical_gate or scheduler_caps_reject_oversized_config or duplicate_active_model_identity or duplicate_active_package_identity or stale_active_db_job_terminal_slurm_sync'`
    -> 7 passed, 517 deselected.
  - `PYTHONDONTWRITEBYTECODE=1 uv run --no-sync pytest -q tests/test_production_scheduler.py -k 'sync_cycle_statuses or active_slurm_status_sync or cancel_active_slurm'`
    -> 10 passed, 514 deselected.
  - `PYTHONDONTWRITEBYTECODE=1 uv run --no-sync pytest -q tests/test_production_scheduler.py -k 'duplicate_candidate_identity'`
    -> 1 passed, 523 deselected.
  - `PYTHONDONTWRITEBYTECODE=1 uv run --no-sync pytest -q tests/test_production_scheduler.py -k 'redacts_secret_urls_and_error_messages or canonical_readiness_query_error'`
    -> 2 passed, 522 deselected.
  - `PYTHONDONTWRITEBYTECODE=1 uv run --no-sync pytest -q tests/test_production_scheduler.py`
    -> 524 passed.
  - Fix-pass evidence for round-1 legacy compatibility findings:
    `PYTHONDONTWRITEBYTECODE=1 uv run --no-sync pytest -q tests/test_production_scheduler.py -k 'monkeypatch or duplicate_candidate_identity or candidate_limit_exceeded or scheduler_caps_reject_oversized_config'`
    -> 7 passed, 517 deselected.
  - `PYTHONDONTWRITEBYTECODE=1 uv run --no-sync ruff check services/orchestrator tests/test_production_scheduler.py`
    -> All checks passed.
  - `openspec validate governance-6-entropy-structural-burndown --strict --no-interactive`
    -> Change is valid.
  - `git diff --check`
    -> passed.
- Non-goals:
  - No discovery/backfill behavior change, candidate-state decision rewrite,
    execution/evidence extraction, lease/reconcile change, retry service
    change, chain stage behavior change, DB schema change, frontend change, or
    `.entropy-baseline` update.
  - No status/reason/evidence key rename.
  - No retirement of scheduler private method/helper compatibility in this
    issue.

### G6-13 Scheduler execution extraction

- Implementation Ready: yes.
- Ownership: `services/orchestrator/scheduler.py`,
  `services/orchestrator/scheduler_execution.py`, forcing/concurrency tests.
- In Scope: Extract forcing production, candidate cohort grouping, concurrent
  submit evidence, runtime-root preflight behavior, and execution orchestration
  helpers that do not own lease or candidate-state semantics.
- Out of Scope: Evidence serialization helper extraction and chain stage
  behavior.
- Tasks: 9.1, 9.2, 9.3, 9.4.
- Dependencies: G6-12.
- PR Boundary: Scheduler execution helpers only.
- Required Reading: `specs/orchestrator-structural-burndown/spec.md`,
  `tests/test_production_scheduler.py`.
- Verification: `uv run --no-sync pytest -q tests/test_production_scheduler.py`
  plus `uv run --no-sync ruff check services/orchestrator tests/test_production_scheduler.py`.
- Acceptance: `run_once` ordering and mutation fences remain stable; focused
  forcing/concurrent candidate tests pass; missing `published_artifact_root` is
  reported as creatable/non-blocking for the control publish stage while other
  missing runtime roots still block before registry, adapter, active-repository,
  or submission work.
- Fixture level: high.
- Repair intensity: high.
- Change surface:
  - `ProductionScheduler.run_once` execution segment after candidate
    construction and before pass-evidence assembly.
  - `ProductionScheduler._produce_forcing_for_candidates`,
    `ProductionScheduler._execute_candidates`, and candidate cohort grouping.
  - New `services/orchestrator/scheduler_execution.py` extraction module.
  - Runtime-root preflight path and execution-boundary evidence consumed by
    `run_once` and `plan-production`.
  - Compatibility aliases/shims in `scheduler.py` for execution helpers used
    by tests or downstream imports.
- Must preserve:
  - Scheduler pass ordering: lock-root preflight, lease acquisition,
    runtime-root preflight, startup reconcile, discovery/candidate building,
    lease-lost fence, pre-execution evidence reservation, Slurm sync/cancel
    checks, forcing production, Slurm preflight, and submission/mutation.
  - Lease-lost and pre-execution evidence mutation fences: no registry,
    adapter, active repository, forcing producer, Slurm preflight, or
    orchestrator submission may run before its existing guard.
  - In-process forcing runs only for ready canonical candidates that need it;
    fresh full-chain/zero-canonical ingestion still skips the forcing producer
    because the Slurm chain produces forcing.
  - Forcing success/failure evidence, `forcing_version_id`, package URI,
    manifest URI, model-run evidence fields, blocked-candidate reason codes,
    and no-mutation proof fields remain unchanged.
  - Candidate cohort grouping preserves restart-compatible grouping,
    source/cycle order, per-source orchestrator factory use, concurrent
    submit bound behavior, overlap receipt evidence, and exception-to-evidence
    conversion.
  - Slurm preflight still blocks before submission with the existing
    `slurm_preflight_blocked` execution boundary and model-run evidence.
  - Runtime-root preflight still treats missing `published_artifact_root` as
    `allow_create=true` and non-blocking/non-mutating in planning, while
    missing workspace, object-store, runtime, temp, lock, or evidence roots
    block before registry, adapter, active repository, forcing, Slurm, or
    submission work.
  - No pass-evidence serialization helper extraction in this issue; evidence
    assembly remains a downstream G6-14 boundary.
- Risk packs considered:
  - Public API / CLI / script entry: selected - `run_once` and
    `plan-production` expose scheduler status, counts, evidence, and dry-run
    behavior.
  - Config / project setup: selected - `dry_run`,
    `slurm_execution_enabled`, `concurrent_submit_bound`, runtime roots,
    service role, and cancel/sync flags drive execution behavior.
  - File IO / path safety / overwrite: selected - runtime root preflight and
    evidence artifact write paths decide whether planning/submission may
    proceed.
  - Schema / columns / units / field names: selected - candidate,
    blocked/skipped, forcing, model-run, Slurm preflight, overlap receipt,
    execution boundary, root-preflight, and no-mutation evidence keys must
    remain stable.
  - Auth / permissions / secrets: selected - Slurm preflight, gateway,
    runtime-root, and forcing/adapter error evidence must preserve existing
    redaction.
  - Concurrency / shared state / ordering: selected - concurrent submit,
    source/cycle cohort grouping, lease-lost fencing, cancellation, active
    Slurm sync, and mutation ordering are core invariants.
  - Resource limits / large input / discovery: selected - concurrent submit
    bounds, model-run evidence caps, and runtime-root preflight must remain
    bounded.
  - Legacy compatibility / examples: selected - private execution helpers and
    `services.orchestrator.scheduler` imports remain available through
    compatibility shims.
  - Error handling / rollback / partial outputs: selected - forcing failure,
    Slurm preflight failure, orchestrator exceptions, cancellation exceptions,
    and root blockers must keep stable blocked/no-mutation outcomes.
  - Release / packaging / dependency compatibility: selected - the new module
    must avoid circular imports with scheduler, scheduler_candidates,
    scheduler_state, scheduler_discovery, and chain.
  - Documentation / migration notes: selected - OpenSpec tasks and PR evidence
    must identify the moved execution boundary and retained non-goals.
- Domain risk packs considered:
  - Hydro-met time series / forcing windows: selected - forcing producer
    inputs use canonical source/cycle, max lead, basin/model identity, and
    canonical policy/source-object identity.
  - SHUD numerical runtime / conservation / NaN: selected - execution passes
    the exact basin payload into the SHUD orchestration chain; no numerical
    behavior may be reinterpreted.
  - Slurm production lifecycle / mock-vs-real parity: selected - Slurm
    preflight, submit/cancel/sync boundaries, overlap receipt, and blocked
    evidence are in scope.
  - Run manifest / QC provenance: selected - execution evidence and
    model-run evidence bind forcing and candidate identity into downstream
    manifests.
  - Published NHMS artifacts / display identity: selected - runtime-root
    preflight distinguishes creatable display artifact root from blocking
    compute/runtime roots.
  - External hydro-met providers / snapshot reproducibility: selected -
    canonical/source-object identity passed to forcing producer must remain
    stable.
  - Geospatial / CRS / basin geometry: not selected - no basin geometry,
    CRS, shapefile, or raster/vector validation changes in this issue.
  - PostGIS / TimescaleDB domain behavior: not selected - no migrations,
    schema, hypertables, or geometry queries change in this issue.
- Invariant Matrix:
  - Governing invariant: execution extraction must not change when a selected
    scheduler candidate mutates state, produces forcing, runs Slurm
    preflight, submits an orchestrator cohort, or blocks with no mutation for
    the same config, roots, candidate state, and repository inputs.
  - Source-of-truth identity/contract: `SchedulerCandidate` identity fields,
    `cycle_id_for(source_id, cycle_time)`, `run_id`, `forcing_version_id`,
    canonical readiness identity, runtime root config, lease token state,
    Slurm preflight result, and `PipelineResult`/`StageRunResult` evidence.
  - Surfaces:
    - Producers: `_build_candidates`, forcing producer,
      `_orchestrator_factory`, Slurm preflight, active repository, scheduler
      config, and runtime-root env/config loaders.
    - Validators/preflight: lease-lost fence, runtime-root preflight,
      pre-execution evidence reservation, active Slurm sync/cancel,
      forcing readiness/failure handling, Slurm preflight, cohort grouping,
      and concurrent submit bound.
    - Storage/cache/query: active repository candidate/active Slurm methods,
      file scheduler lease, pre-execution evidence artifacts, and object-store
      config only through existing interfaces; no DB schema changes.
    - Public routes/entrypoints: `ProductionScheduler.run_once`,
      `ProductionScheduler._produce_forcing_for_candidates`,
      `ProductionScheduler._execute_candidates`,
      `ProductionScheduler._execute_candidate_cohort`,
      `plan-production`, and imports from `services.orchestrator.scheduler`.
    - Frontend/downstream consumers: ops scheduler evidence, model-run
      evidence, run manifests, retry/cancel consumers, and published display
      artifact planning evidence; no frontend code change.
    - Failure paths/rollback/stale state: missing/invalid roots, lease lost,
      forcing producer unavailable/failure, canonical forcing identity
      mismatch, Slurm preflight blockers, active Slurm cancel/sync failures,
      orchestrator exceptions, concurrent submit exceptions, and dry-run
      planning.
    - Evidence/audit/readiness: `execution_boundary`, `root_preflight`,
      `no_mutation_proof`, `pre_execution_evidence`, `forcing_production`,
      `model_run_evidence`, `slurm_preflight`,
      `submit_overlap_receipt`, counts/status, and final readiness flags.
  - Regression rows:
    - Ready canonical candidate with forcing producer -> producer is invoked
      before orchestration with identical source/cycle/model/basin/canonical
      identity, produced `forcing_version_id` reaches candidate, basin
      payload, and model-run evidence.
    - Forcing producer failure -> candidate blocks with
      `forcing_production_blocked`, orchestrator is not called, status is
      `preflight_blocked`, and no-mutation proof remains false for SHUD
      runtime.
    - Fresh zero-canonical full-chain candidate -> in-process forcing
      producer is skipped, restart stage is suppressed, and orchestration
      payload stays full-chain.
    - Two restart-compatible cohorts with `concurrent_submit_bound > 1` ->
      submissions overlap, both evidence rows are returned, candidate order
      remains stable, and overlap receipt is recorded.
    - `concurrent_submit_bound == 1` or a single cohort -> execution remains
      sequential and evidence shape/order is unchanged.
    - Slurm preflight blocker -> no orchestrator submission, execution
      boundary is `slurm_preflight_blocked`, candidate/model-run evidence
      remains blocked, and counts report zero submitted.
    - Missing `published_artifact_root` with all other roots valid in dry-run
      planning -> root preflight is ready, check has `allow_create=true`,
      the directory is not created, and registry/adapter path may plan
      without mutation.
    - Missing workspace, object-store, runtime, temp, lock, or evidence root
      -> root preflight returns the existing blocker code before registry,
      adapter, active repository, forcing producer, Slurm preflight, or
      orchestrator submission.
    - Lease loss after candidate construction -> pre-execution evidence
      reservation and submission/mutation remain blocked.
    - Active Slurm cancel/sync paths -> execution extraction does not move
      candidate-state ownership; cancel/sync evidence and replacement
      submission behavior remain unchanged.
    - Orchestrator exception inside one cohort -> existing error evidence and
      status mapping remain stable without dropping sibling cohort evidence.
- Boundary-surface checklist:
  - Shared helper roots: execution helpers moved to `scheduler_execution.py`;
    scheduler pass/evidence helpers remain in `scheduler.py` unless needed as
    compatibility wrappers.
  - Public entrypoints: `run_once`, execution private methods,
    `plan-production`, and legacy imports from `services.orchestrator.scheduler`.
  - Read surfaces: scheduler config, selected candidates, active repository,
    runtime roots, canonical/forcing identity, Slurm preflight config, and
    orchestrator factory.
  - Write/delete/overwrite surfaces: pre-execution evidence artifacts,
    scheduler evidence artifact path, active repository mutation via existing
    orchestrator/submission path, and runtime-root directory creation checks.
  - Staging/publish/rollback surfaces: published artifact root preflight only;
    no publish-stage extraction or display artifact writes in G6-13.
  - Producer/consumer evidence boundaries: candidate dictionaries,
    forcing evidence, blocked candidate evidence, Slurm preflight evidence,
    model-run evidence, overlap receipt, and root-preflight evidence.
  - Stale-state/idempotency boundaries: lease-lost fence, pre-execution
    reservation, active Slurm cancel/sync, restart-compatible cohorts,
    concurrent submit bound, and dry-run/no-mutation planning.
  - Unchanged downstream consumers: scheduler candidate construction,
    scheduler evidence serialization, retry service, chain stage execution,
    reservation/reconcile protocols, DB schema, frontend, and
    `.entropy-baseline`.
- Required evidence:
  - `PYTHONDONTWRITEBYTECODE=1 uv run --no-sync pytest -q tests/test_production_scheduler.py -k 'scheduler_invokes_forcing_producer_before_orchestration_for_ready_canonical_candidate or scheduler_blocks_orchestration_when_forcing_producer_fails or scheduler_propagates_produced_forcing_identity_to_orchestration or fresh_cycle_with_zero_canonical_runs_full_chain_without_in_process_forcing'`
    -> forcing success/failure/identity propagation and fresh full-chain
    forcing skip tests pass.
  - `PYTHONDONTWRITEBYTECODE=1 uv run --no-sync pytest -q tests/test_production_scheduler.py -k 'concurrent_candidates_submits_overlap'`
    -> concurrent submit overlap receipt and candidate evidence order pass.
  - `PYTHONDONTWRITEBYTECODE=1 uv run --no-sync pytest -q tests/test_production_scheduler.py -k 'slurm_preflight_blocks_missing_or_localhost_database_before_submission or issue_196_blocked_preflight_evidence_keeps_existing_consumers_stable or cancel_active_slurm_blocks_before_cancel_when_final_evidence_artifact_exists'`
    -> Slurm preflight/cancel blocked paths remain no-submit and evidence
    compatible.
  - `PYTHONDONTWRITEBYTECODE=1 uv run --no-sync pytest -q tests/test_production_scheduler.py -k 'no_flag_missing_published_artifact_root_is_created_by_control_publish_stage or no_flag_invalid_env_roots_block_before_registry_adapter_or_submit or no_flag_missing_allowed_roots_blocks_before_registry_adapter_or_submit'`
    -> runtime-root preflight published-root allow-create and other-root
    blockers remain stable.
  - `PYTHONDONTWRITEBYTECODE=1 uv run --no-sync pytest -q tests/test_production_scheduler.py`
    -> full production scheduler tests pass because this extraction touches
    shared execution ordering and compatibility shims.
  - `PYTHONDONTWRITEBYTECODE=1 uv run --no-sync pytest -q tests/test_entropy_audit_script.py -k 'services_orchestrator'`
    -> orchestrator module-count governance expectation is updated if the new
    module changes the entropy audit count.
  - `PYTHONDONTWRITEBYTECODE=1 uv run --no-sync ruff check services/orchestrator tests/test_production_scheduler.py tests/test_entropy_audit_script.py`
    -> lint passes.
  - `openspec validate governance-6-entropy-structural-burndown --strict --no-interactive`
    -> valid.
  - `git diff --check`
    -> no whitespace errors.
- Implementation evidence (2026-06-15):
  - `PYTHONDONTWRITEBYTECODE=1 uv run --no-sync pytest -q tests/test_production_scheduler.py -k 'scheduler_invokes_forcing_producer_before_orchestration_for_ready_canonical_candidate or scheduler_blocks_orchestration_when_forcing_producer_fails or scheduler_propagates_produced_forcing_identity_to_orchestration or fresh_cycle_with_zero_canonical_runs_full_chain_without_in_process_forcing'`
    -> 4 passed, 522 deselected.
  - `PYTHONDONTWRITEBYTECODE=1 uv run --no-sync pytest -q tests/test_production_scheduler.py -k 'concurrent_candidates_submits_overlap or concurrent_submit_bound or sibling_cohort or mixed_cohort or one_cohort'`
    -> 3 passed, 523 deselected.
  - `PYTHONDONTWRITEBYTECODE=1 uv run --no-sync pytest -q tests/test_production_scheduler.py -k 'slurm_preflight_blocks_missing_or_localhost_database_before_submission or issue_196_blocked_preflight_evidence_keeps_existing_consumers_stable or cancel_active_slurm_blocks_before_cancel_when_final_evidence_artifact_exists'`
    -> 32 passed, 494 deselected.
  - `PYTHONDONTWRITEBYTECODE=1 uv run --no-sync pytest -q tests/test_production_scheduler.py -k 'no_flag_missing_published_artifact_root_is_created_by_control_publish_stage or no_flag_invalid_env_roots_block_before_registry_adapter_or_submit or no_flag_missing_allowed_roots_blocks_before_registry_adapter_or_submit'`
    -> 8 passed, 518 deselected.
  - `PYTHONDONTWRITEBYTECODE=1 uv run --no-sync pytest -q tests/test_entropy_audit_script.py -k 'services_orchestrator'`
    -> 1 passed, 191 deselected.
  - `PYTHONDONTWRITEBYTECODE=1 uv run --no-sync ruff check services/orchestrator tests/test_production_scheduler.py tests/test_entropy_audit_script.py`
    -> All checks passed.
  - `openspec validate governance-6-entropy-structural-burndown --strict --no-interactive`
    -> Change is valid.
  - `git diff --check`
    -> passed.
  - `PYTHONDONTWRITEBYTECODE=1 uv run --no-sync pytest -q tests/test_production_scheduler.py`
    -> 526 passed.
- Non-goals:
  - No candidate construction, candidate-state decision, discovery/backfill,
    evidence serialization, reservation/reconcile, retry service, chain stage,
    DB schema, frontend, or `.entropy-baseline` update.
  - No status/reason/evidence key rename.
  - No retirement of scheduler private method/helper compatibility in this
    issue.

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
- Fixture level: high.
- Repair intensity: high.
- Change surface:
  - `ProductionScheduler.run_once` pass-evidence assembly after discovery,
    candidate construction, execution/cancel/sync handling, and before final
    evidence artifact write.
  - `ProductionScheduler._base_evidence`,
    `ProductionScheduler._write_prelock_blocked_evidence`,
    `ProductionScheduler._reserve_pre_execution_evidence`, and
    `ProductionScheduler._write_evidence`.
  - Scheduler evidence helpers for candidate/cancel/sync
    evidence-write-blocked payloads, execution write proof, mutation proof,
    evidence write error payloads, artifact no-clobber/no-follow helpers, and
    bounded evidence serialization.
  - New `services/orchestrator/scheduler_evidence.py` extraction module.
  - Compatibility imports/shims in `services/orchestrator/scheduler.py` for
    existing private helper and monkeypatch/import surfaces.
- Must preserve:
  - Scheduler pass ordering: lock-root preflight, lease acquisition,
    runtime-root preflight, startup reconcile, discovery/candidate building,
    lease-lost fence, pre-execution evidence reservation, Slurm sync/cancel
    checks, forcing/execution, and final evidence write.
  - Pre-execution evidence reservation occurs after the lease-lost fence and
    before any Slurm status-sync, Slurm cancellation, forcing producer,
    Slurm preflight, orchestrator submission, pipeline status write, or
    pipeline event write.
  - Evidence reservation uses the existing schema version, `pass_id`,
    `started_at`, `reserved_at`, `status`, `candidate_count`,
    `artifact_path`, `final_evidence_artifact`, and proof key values.
  - Evidence artifact path safety stays descriptor-bound: evidence directory
    containment, final-component safety, final evidence no-clobber,
    pre-execution no-clobber, symlink no-follow, and non-regular artifact
    rejection remain stable.
  - Bounded evidence still returns `status: resource_limit_blocked`, preserves
    review/readiness/runtime-root/preflight/pre-execution proof fields, drops
    oversized candidate/source/model-run lists, and stays parseable.
  - Final evidence keeps all existing keys and status/reason/schema values:
    `review_contract`, `production_contract`, `operator_filters`, `filters`,
    `model_discovery`, `source_cycles`, candidates/blocked/skipped,
    `duplicate_exclusions`, `counts`, `model_run_evidence`,
    `execution_write_proof`, Slurm sync/cancel proofs, `no_mutation_proof`,
    `execution_boundary`, `restart_reconcile`, `submit_overlap_receipt`,
    `slurm_preflight`, `evidence_pre_execution`, `root_preflight`,
    `backfill`, `retention`, and `artifact_path`.
  - Startup restart reconcile evidence remains attached to final pass evidence
    without moving reconcile ownership or changing reserved-unbound job
    semantics.
  - `scheduler.py` remains the compatibility surface for existing private
    helper calls and monkeypatch paths until a later migration issue.
  - No execution orchestration, candidate construction, lease, chain,
    reservation/reconcile protocol, retry service, DB schema, frontend, or
    `.entropy-baseline` behavior changes.
- Risk packs considered:
  - Public API / CLI / script entry: selected - `run_once`,
    `run_continuous`, `plan-production`, and ops evidence consumers observe
    evidence status, counts, artifact paths, and readiness fields.
  - Config / project setup: selected - `dry_run`, root config, evidence dir,
    resource limits, Slurm sync/cancel flags, retention, and backfill settings
    influence evidence shape.
  - File IO / path safety / overwrite: selected - final and pre-execution
    evidence artifacts are generated files under an operator-configured
    evidence root and must not follow symlinks or overwrite existing files.
  - Schema / columns / units / field names: selected - pass evidence,
    pre-execution evidence, readiness, proof, root-preflight, and bounded
    evidence keys are downstream contracts.
  - Auth / permissions / secrets: selected - evidence serialization must keep
    existing redaction behavior for signed URIs, credentials, and Slurm/runtime
    details.
  - Concurrency / shared state / ordering: selected - evidence reservation is
    a mutation fence before sync/cancel/submit and must be stable under
    concurrent scheduler passes and existing artifact races.
  - Resource limits / large input / discovery: selected - final evidence may
    exceed `MAX_EVIDENCE_BYTES` and must reduce to a bounded payload without
    unbounded recursive or oversized JSON behavior.
  - Legacy compatibility / examples: selected - private evidence helpers and
    imports from `services.orchestrator.scheduler` remain available through
    shims.
  - Error handling / rollback / partial outputs: selected - evidence write
    failure, existing artifact, unsafe path, symlink, non-regular file, and
    reservation-blocked paths must produce stable blocked evidence or typed
    errors without partial mutation.
  - Release / packaging / dependency compatibility: selected - the new module
    must avoid circular imports with scheduler, scheduler_state,
    scheduler_candidates, scheduler_discovery, scheduler_execution, and chain.
  - Documentation / migration notes: selected - OpenSpec tasks and PR evidence
    must describe moved evidence ownership and retained non-goals.
- Domain risk packs considered:
  - Slurm production lifecycle / mock-vs-real parity: selected - Slurm
    sync/cancel/preflight/submission proofs in final evidence remain the audit
    boundary for real and mocked lifecycle paths.
  - Run manifest / QC provenance: selected - scheduler pass evidence binds
    candidate identity, model-run evidence, write proofs, and restart reconcile
    evidence consumed by downstream run/QC review.
  - Published NHMS artifacts / display identity: selected - runtime-root and
    artifact path evidence must remain attached so display artifact planning is
    traceable.
  - Hydro-met time series / forcing windows: selected - source/cycle evidence
    and candidate identity fields must remain stable when evidence assembly is
    moved.
  - External hydro-met providers / snapshot reproducibility: selected -
    source-object and provider-derived cycle evidence must not be renamed or
    dropped from pass evidence.
  - Geospatial / CRS / basin geometry: not selected - no geometry, CRS,
    shapefile, or raster/vector validation changes in this issue.
  - SHUD numerical runtime / conservation / NaN: not selected - no model
    runtime or numerical output interpretation changes in this issue.
  - PostGIS / TimescaleDB domain behavior: not selected - no migrations,
    hypertables, geometry queries, or DB schema changes in this issue.
- Invariant Matrix:
  - Governing invariant: evidence extraction must preserve exactly when the
    scheduler proves evidence writability, how it serializes pass evidence, and
    which evidence keys prove mutation/no-mutation for the same config,
    candidate state, roots, lease state, and execution outcomes.
  - Source-of-truth identity/contract: `pass_id`, scheduler evidence schema
    version, pre-execution reservation schema version, evidence root identity,
    final artifact basename, bounded evidence contract, candidate identity,
    Slurm sync/cancel/submit proofs, and restart reconcile evidence.
  - Surfaces:
    - Producers: `ProductionScheduler.run_once`, `_base_evidence`,
      `_reserve_pre_execution_evidence`, execution/cancel/sync evidence
      payload builders, restart reconcile, retention/backfill evidence, and
      bounded evidence builder.
    - Validators/preflight: lock-root and runtime-root preflight,
      lease-lost fence, evidence directory/root containment,
      `_require_evidence_artifact_available`, `_write_new_regular_file`,
      evidence safe/redaction conversion, and `MAX_EVIDENCE_BYTES` bounding.
    - Storage/cache/query: evidence directory artifacts, pre-execution
      reservation artifact, final pass artifact, active repository evidence
      payloads only through existing interfaces; no DB schema changes.
    - Public routes/entrypoints: `ProductionScheduler.run_once`,
      `ProductionScheduler.run_continuous`, private evidence helper shims in
      `scheduler.py`, `plan-production`, and imports from
      `services.orchestrator.scheduler`.
    - Frontend/downstream consumers: ops scheduler evidence, readiness review,
      run manifest/QC review, retry/cancel/status-sync consumers, and display
      artifact planning evidence; no frontend code change.
    - Failure paths/rollback/stale state: evidence dir symlink/traversal,
      existing final artifact, existing reservation artifact, final artifact
      symlink, non-regular artifact, oversized evidence, reservation-blocked
      sync/cancel/submit, startup reserved-unbound reconcile, and resource
      limit blocked paths.
    - Evidence/audit/readiness: final pass JSON, pre-execution reservation
      JSON, `evidence_write_error`, bounded payload, `readiness`,
      `execution_write_proof`, Slurm proofs, `no_mutation_proof`,
      `restart_reconcile`, `root_preflight`, `retention`, and audit tests.
  - Regression rows:
    - Valid non-dry-run candidate with required mutation ->
      pre-execution reservation artifact is written before sync/cancel/forcing/
      Slurm/orchestrator mutation, final evidence includes
      `evidence_pre_execution`, and execution write proof reports protected
      mutation.
    - Reservation write blocked by existing/unsafe artifact -> no sync/cancel/
      forcing/Slurm/orchestrator mutation is attempted, candidate/cancel/sync
      evidence uses existing blocked reason keys, pass status is preflight
      blocked, and final evidence remains conservative.
    - Lock-root or runtime-root preflight blocked before lease/execution ->
      evidence writes only when the evidence root is safe and writable, and
      root-preflight, no-mutation, counts, and execution boundary stay stable.
    - Final evidence artifact exists or is a symlink/non-regular file ->
      writer does not follow or overwrite it and returns the existing typed
      evidence write error semantics.
    - Oversized pass evidence -> bounded payload is written with
      `resource_limit_blocked`, preserves readiness/root/pre-execution/proof
      fields, removes unbounded lists, and stays within byte limit.
    - Startup reserved-unbound job reconcile -> `restart_reconcile` evidence is
      still included in final pass evidence and stale reserved jobs are not
      resubmitted by evidence extraction.
    - Slurm status sync and cancellation paths -> reservation proof is visible
      before mutation, sync/cancel proofs keep `unknown_after_attempt` and
      blocked states, and no-mutation proof reflects the same writes/calls as
      before extraction.
    - Existing private helper import or monkeypatch through
      `services.orchestrator.scheduler` -> compatibility shim delegates to the
      extracted helper without changing payload shape.
- Boundary-surface checklist:
  - Shared helper roots: evidence helpers move to `scheduler_evidence.py`;
    execution helpers remain in `scheduler_execution.py`; scheduler pass still
    owns orchestration ordering.
  - Public entrypoints: `run_once`, `run_continuous`, evidence private method
    shims, helper imports from `services.orchestrator.scheduler`, and
    `plan-production`.
  - Read surfaces: scheduler config, root paths, candidates/skipped/blocked
    evidence, execution/cancel/sync evidence, restart reconcile evidence,
    retention/backfill evidence, and existing artifact state.
  - Write/delete/overwrite surfaces: final evidence artifact,
    pre-execution reservation artifact, evidence-write error payload only; no
    delete or overwrite behavior is introduced.
  - Producer/consumer evidence boundaries: base pass evidence, pre-execution
    reservation, blocked candidate/cancel/sync evidence, execution write proof,
    Slurm proofs, bounded evidence, final pass artifact, ops/readiness
    consumers.
  - Stale-state/idempotency boundaries: lease-lost fence, existing artifact
    no-clobber, startup reserved-unbound reconcile, status-sync
    unknown-after-attempt, cancellation blocked/unknown states, and dry-run
    no-mutation planning.
  - Unchanged downstream consumers: scheduler lease/state/discovery/candidates/
    execution modules, chain stage execution, reservation/reconcile protocols,
    retry service, DB schema, frontend, docs/runbooks, and `.entropy-baseline`.
- Required evidence:
  - `PYTHONDONTWRITEBYTECODE=1 uv run --no-sync pytest -q tests/test_production_scheduler.py -k 'evidence_dir_symlink_cannot_escape_workspace or evidence_final_artifact_symlink_is_not_followed or evidence_existing_artifact_file_is_not_overwritten'`
    -> evidence directory containment, final artifact no-follow, and
    no-clobber tests pass.
  - `PYTHONDONTWRITEBYTECODE=1 uv run --no-sync pytest -q tests/test_production_scheduler.py -k 'scheduler_evidence_context_accepts_exported_keyword_callbacks or scheduler_evidence_private_helper_compatibility_shims_delegate or scheduler_evidence_module_imports_without_scheduler_cycle'`
    -> direct scheduler-evidence context callbacks, compatibility shims, and
    circular-import-free module import pass.
  - `PYTHONDONTWRITEBYTECODE=1 uv run --no-sync pytest -q tests/test_production_scheduler.py -k 'normal_mutation_sees_pre_execution_reservation_before_forcing_and_submit or sync_cycle_statuses_sees_pre_execution_reservation_before_mutating or sync_cycle_statuses_blocks_before_sync_when_pre_execution_reservation_fails or cancel_active_slurm_blocks_before_cancel_when_final_evidence_artifact_exists or pre_execution_existing_regular_artifact_blocks_before_forcing_and_submit or pre_execution_symlink_artifact_blocks_before_status_sync_and_preserves_target or pre_execution_non_regular_artifact_blocks_before_cancel'`
    -> reservation ordering, sync/cancel/forcing/orchestrator mutation fence,
    pre-execution artifact no-clobber/no-follow/non-regular rejection, and
    conservative blocked evidence tests pass.
  - `PYTHONDONTWRITEBYTECODE=1 uv run --no-sync pytest -q tests/test_production_scheduler.py -k 'bounded_evidence_preserves_no_flag_root_runtime_and_preflight_proof or no_flag_resource_limit_evidence_retains_runtime_root_preflight_proof or bounded_evidence_preserves_pre_execution_reservation_proof'`
    -> bounded evidence keeps required preflight/readiness/reservation proofs.
  - `PYTHONDONTWRITEBYTECODE=1 uv run --no-sync pytest -q tests/test_production_scheduler.py -k 'scheduler_pass_startup_reconciles_reserved_unbound_jobs or restart_reconcile'`
    -> startup reconcile evidence remains attached and reserved-unbound jobs
    are not resubmitted.
  - `PYTHONDONTWRITEBYTECODE=1 uv run --no-sync pytest -q tests/test_production_scheduler.py -k 'scheduler_evidence_redacts_signed_candidate_outcome_log_uri or scheduler_evidence_redacts_sensitive_runtime_payloads'`
    -> signed candidate outcome log URIs, credential-bearing runtime values,
    and Slurm/runtime payload inputs keep the existing redacted evidence shape
    after serialization helper extraction.
  - `PYTHONDONTWRITEBYTECODE=1 uv run --no-sync pytest -q tests/test_production_scheduler.py -k 'scheduler_evidence_private_helper_compatibility_shims_delegate or scheduler_evidence_module_imports_without_scheduler_cycle'`
    -> imports from `services.orchestrator.scheduler` and the new
    `services.orchestrator.scheduler_evidence` module work without circular
    import failure, and private helper shims delegate to extracted helpers
    without changing payload shape.
  - `PYTHONDONTWRITEBYTECODE=1 uv run --no-sync pytest -q tests/test_production_scheduler.py`
    -> full production scheduler tests pass because this extraction touches
    shared evidence contracts and compatibility shims.
  - `PYTHONDONTWRITEBYTECODE=1 uv run --no-sync pytest -q tests/test_entropy_audit_script.py -k 'services_orchestrator'`
    -> orchestrator module-count governance expectation is updated if the new
    module changes the entropy audit count.
  - `PYTHONDONTWRITEBYTECODE=1 uv run --no-sync ruff check services/orchestrator tests/test_production_scheduler.py tests/test_entropy_audit_script.py`
    -> lint passes.
  - `openspec validate governance-6-entropy-structural-burndown --strict --no-interactive`
    -> valid.
  - `git diff --check`
    -> no whitespace errors.
- Implementation evidence (2026-06-15):
  - Extracted scheduler pass evidence assembly, pre-execution reservation,
    evidence artifact guards, write-error payloads, proof/count helpers, and
    bounded serialization into `services/orchestrator/scheduler_evidence.py`.
  - Kept `services/orchestrator/scheduler.py` as the compatibility surface:
    old private method/helper names delegate to extracted helpers, and
    pre-execution reservation still injects the scheduler-module file-write
    helpers so existing monkeypatch paths remain effective.
  - Added focused regression tests for signed outcome log URI redaction,
    sensitive runtime/Slurm payload redaction, private helper compatibility
    delegation, direct keyword-compatible context callbacks, reservation before
    normal forcing/orchestrator mutation, pre-execution artifact
    no-clobber/no-follow/non-regular rejection, and circular-import-free
    `scheduler_evidence` import.
  - Updated the services/orchestrator entropy file-count expectation for the
    new tracked scheduler evidence module.
  - Verification:
    `PYTHONDONTWRITEBYTECODE=1 uv run --no-sync pytest -q tests/test_production_scheduler.py -k 'evidence_dir_symlink_cannot_escape_workspace or evidence_final_artifact_symlink_is_not_followed or evidence_existing_artifact_file_is_not_overwritten'`
    -> `4 passed, 525 deselected`.
  - Verification:
    `PYTHONDONTWRITEBYTECODE=1 uv run --no-sync pytest -q tests/test_production_scheduler.py -k 'scheduler_evidence_context_accepts_exported_keyword_callbacks or normal_mutation_sees_pre_execution_reservation_before_forcing_and_submit or pre_execution_existing_regular_artifact_blocks_before_forcing_and_submit or pre_execution_symlink_artifact_blocks_before_status_sync_and_preserves_target or pre_execution_non_regular_artifact_blocks_before_cancel'`
    -> `5 passed, 529 deselected`.
  - Verification:
    `PYTHONDONTWRITEBYTECODE=1 uv run --no-sync pytest -q tests/test_production_scheduler.py -k 'pre_execution_reservation or scheduler_evidence_context or scheduler_evidence_private_helper_compatibility_shims_delegate or scheduler_evidence_module_imports_without_scheduler_cycle'`
    -> `7 passed, 527 deselected`.
  - Verification:
    `PYTHONDONTWRITEBYTECODE=1 uv run --no-sync pytest -q tests/test_production_scheduler.py -k 'bounded_evidence_preserves_no_flag_root_runtime_and_preflight_proof or no_flag_resource_limit_evidence_retains_runtime_root_preflight_proof or bounded_evidence_preserves_pre_execution_reservation_proof'`
    -> `3 passed, 526 deselected`.
  - Verification:
    `PYTHONDONTWRITEBYTECODE=1 uv run --no-sync pytest -q tests/test_production_scheduler.py -k 'scheduler_pass_startup_reconciles_reserved_unbound_jobs or restart_reconcile'`
    -> `1 passed, 528 deselected`.
  - Verification:
    `PYTHONDONTWRITEBYTECODE=1 uv run --no-sync pytest -q tests/test_production_scheduler.py -k 'scheduler_evidence_redacts_signed_candidate_outcome_log_uri or scheduler_evidence_redacts_sensitive_runtime_payloads or scheduler_evidence_private_helper_compatibility_shims_delegate or scheduler_evidence_module_imports_without_scheduler_cycle'`
    -> `4 passed, 525 deselected`.
  - Verification:
    `PYTHONDONTWRITEBYTECODE=1 uv run --no-sync pytest -q tests/test_production_scheduler.py`
    -> `529 passed`.
  - Verification:
    `PYTHONDONTWRITEBYTECODE=1 uv run --no-sync pytest -q tests/test_entropy_audit_script.py -k 'services_orchestrator'`
    -> `1 passed, 191 deselected`.
  - Verification:
    `PYTHONDONTWRITEBYTECODE=1 uv run --no-sync ruff check services/orchestrator tests/test_production_scheduler.py tests/test_entropy_audit_script.py`
    -> `All checks passed!`.
  - Verification:
    `openspec validate governance-6-entropy-structural-burndown --strict --no-interactive`
    -> valid.
  - Verification: `git diff --check` -> no whitespace errors.
- Non-goals:
  - No execution orchestration, forcing production, candidate construction,
    discovery/backfill, lease, chain, reservation/reconcile protocol, retry
    service, DB schema, frontend, docs/runbooks, or `.entropy-baseline`
    update.
  - No status, reason, error code, schema version, evidence-key, readiness-key,
    or artifact-name rename.
  - No retirement of scheduler private method/helper compatibility in this
    issue.

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

### G6-20 PR #481 production copyback/runtime finalization

- Implementation Ready: yes.
- Ownership: `services/tile_publisher/publisher.py`,
  `tests/test_tile_publisher.py`, two-node runtime validation/tests, env
  examples, two-node docs/runbooks, and this OpenSpec fixture.
- In Scope: Finalize the approved production behavior for
  `NHMS_OBJECT_STORE_COPYBACK_ROOT`: no-follow source/root validation,
  complete run-tree validation, exact-root equality semantics, rollback-safe
  replacement, copyback-before-publication visibility, overlap rejection,
  `ObjectStoreError` normalization, display-forbidden role boundary, and
  docs/tests evidence.
- Out of Scope: New display features, new storage backends, or entropy baseline
  rewrites.
- Tasks:
  - [ ] 16.1 Preserve raw configured copyback root until no-follow validation
    rejects symlink components; compare only verified real paths for equality,
    overlap, and containment.
  - [ ] 16.2 Validate every `runs/<run_id>` tree for manifest/output/log
    completeness even when copyback root exactly equals object-store root.
  - [ ] 16.3 Replace canonical copyback run trees with rollback-safe sibling
    staging/backup semantics so failed promotion cannot expose partial run
    products.
  - [ ] 16.4 Stage q_down display artifacts until copyback succeeds; failed
    first publish exposes no new manifest and failed republish leaves the
    previous manifest/cycle pointer unchanged.
  - [ ] 16.5 Keep compute-only runtime/env validation and docs aligned so
    `display_readonly` cannot configure copyback or other compute-control path
    env.
  - [ ] 16.6 Verify with focused copyback, full tile publisher, runtime/static
    Docker tests, ruff, strict OpenSpec validation, and `git diff --check`.
- Dependencies: PR #481 issue #460 closure under epic #456.
- PR Boundary: Approved local production copyback/runtime hardening only.
- Required Reading: this addendum, `services/tile_publisher/publisher.py`,
  `tests/test_tile_publisher.py`, runtime mode/static Docker validation tests,
  and two-node env/docs.
- Acceptance: confirmed copyback/runtime blockers close as a class-level fix and
  forbidden files `.entropy-baseline/latest.json` and
  `docs/runbooks/current-production-ops.md` remain untouched.
