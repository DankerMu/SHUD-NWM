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
- [ ] 9.3 Preserve scheduler runtime-root preflight semantics: missing
  `published_artifact_root` is a control publish-stage creatable root, while
  missing workspace, object-store, runtime, temp, lock, and evidence roots still
  block before registry, adapter, active-repository, or submission work.
- [ ] 9.4 Verify with focused forcing, concurrent candidate, and runtime-root
  preflight tests.

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
