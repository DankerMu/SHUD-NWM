## Context

The audit found tracked placeholder and legacy paths:

- `apps/web` is explicitly a deprecated frontend placeholder.
- Hyphenated worker directories such as `workers/forcing-producer` and `workers/shud-runtime` are placeholders; canonical packages use underscores.
- `workers/sbatch_templates` contains legacy single-run templates and should be treated more carefully than empty placeholders because `services/slurm_gateway/config.py` references its legacy status.
- `services/tile-publisher` exists while active tile publisher code is elsewhere.
- QHH scripts are diagnostic-only and still useful for bring-up/debugging.
- Playwright specs use `page.route('**/api/v1/**')` and should not be called live e2e.
- `.github/workflows/ci.yml` contains a paused job with `&& false`.

## Decisions

### D0. Issue #362 slice is inventory-only

This change fixture spans the whole Governance-2 epic, but issue #362 only
creates the persistent legacy/dead-code inventory. It MUST NOT delete, move,
rename, archive, or wrap any governed path. Later issues #363-#366 perform the
actual cleanup, diagnostic isolation, e2e split, and paused CI retirement after
the inventory exists.

Fixture level for #362: `expanded` because the work classifies legacy paths and
future cleanup evidence across docs, CI, frontend tests, workers, Slurm, and
diagnostic scripts.

Repair intensity for #362: `medium`. The PR is documentation/governance-only,
but incorrect classification could misdirect later destructive cleanup.

Risk packs considered for #362:

- Public API / CLI / script entry: selected - inventory must identify scripts
  and test commands that remain active entrypoints or diagnostic commands.
- Config / project setup: selected - inventory must classify workflow and
  deployment/config references that make a path active.
- File IO / path safety / overwrite: not selected - #362 does not add runtime
  file reads/writes or move/delete/archive paths.
- Schema / columns / units / field names: selected - the persistent inventory
  has a governed row schema used by downstream #363-#366 cleanup issues.
- Auth / permissions / secrets: not selected - no auth policy, secret handling,
  or role permission behavior changes.
- Concurrency / shared state / ordering: not selected - no scheduler/runtime
  state changes.
- Resource limits / large input / discovery: selected - inventory evidence uses
  repository-wide discovery and should avoid unbounded or misleading claims.
- Legacy compatibility / examples: selected - primary risk; every governed path
  must have evidence before later retirement.
- Error handling / rollback / partial outputs: not selected - no runtime side
  effects in this PR.
- Release / packaging / dependency compatibility: selected - inventory must
  record active build/import/deploy evidence before later cleanup.
- Documentation / migration notes: selected - the inventory is the migration
  source for later G2 issues.
- Geospatial / CRS / basin geometry: not selected - no GIS behavior change.
- Hydro-met time series / forcing windows: not selected - no forcing/time-series
  behavior change.
- SHUD numerical runtime / conservation / NaN: not selected - no solver/runtime
  behavior change.
- PostGIS / TimescaleDB domain behavior: not selected - no database behavior
  change.
- Slurm production lifecycle / mock-vs-real parity: selected - inventory covers
  `workers/sbatch_templates`, `infra/sbatch`, and Slurm gateway references.
- External hydro-met providers / snapshot reproducibility: not selected - no
  provider behavior change.
- Run manifest / QC provenance: not selected - no production manifest/QC
  behavior change.
- Published NHMS artifacts / display identity: selected - inventory covers tile
  publisher and display evidence boundaries.

Must preserve for #362:

- No governed path is deleted, moved, renamed, archived, or wrapped.
- No CI workflow, Playwright runtime behavior, QHH script behavior, Slurm
  gateway default, frontend app behavior, API behavior, or deployment behavior
  changes.
- Follow-up tasks #363-#366 must be able to use the inventory without treating
  #362 as cleanup approval.

### D1. Use four path statuses

Each governed path should be classified as:

- `production`: active runtime/build/deploy path.
- `diagnostic`: supported debugging or bring-up path, not production.
- `test-only`: regression fixtures or mocked tests.
- `archived`: historical reference outside active build/test/deploy.

The inventory must be a persistent artifact, not just PR body prose. Each row should include exact path, status, owner area, active build/import/deploy evidence, docs/runbook migration, and final action.

### D1B. Issue #363 retires placeholder entrypoints after inventory proof

Issue #363 applies only the inventory-backed cleanup for legacy placeholder
directories and legacy Slurm template placement:

- Remove or archive `apps/web`, hyphenated worker placeholders, and
  `services/tile-publisher` so they no longer appear as active application or
  Python package entrypoints.
- Treat `workers/sbatch_templates` separately because it contains real legacy
  `.sbatch` files. If the directory is removed from active worker space, preserve
  the template names, canonical `infra/sbatch` replacements, and migration notes
  under `docs/archived/**` before deletion.
- Update current docs and governance inventories in the same PR. Historical
  OpenSpec records may remain historical if they are clearly not current source
  of truth.
- Do not change QHH diagnostic scripts, mocked Playwright specs, or paused CI
  workflow behavior in #363.

Fixture level for #363: `expanded`. The issue removes or archives tracked paths,
touches legacy compatibility/examples, and changes current documentation
entrypoints.

Repair intensity for #363: `high`. The work has file deletion/archive behavior
and must preserve active runtime/build/import/deploy contracts while retiring
legacy paths.

Risk packs considered for #363:

- Public API / CLI / script entry: selected - cleanup must not remove active
  entrypoints or console scripts and must prove canonical paths remain active.
- Config / project setup: selected - project profile, module index, inventory,
  and Slurm template settings must agree on active paths.
- File IO / path safety / overwrite: selected - the PR deletes or archives
  tracked files and must avoid deleting active counterparts or generated
  evidence.
- Schema / columns / units / field names: not selected - no DB/API/schema fields
  change.
- Auth / permissions / secrets: not selected - no auth, role permission, or
  secret handling change.
- Concurrency / shared state / ordering: not selected - no runtime state
  transition change.
- Resource limits / large input / discovery: selected - verification scans must
  avoid generated trees and prove no active references remain without relying on
  stale broad grep claims.
- Legacy compatibility / examples: selected - this is the primary risk; archived
  evidence and migration notes must keep historical intent understandable.
- Error handling / rollback / partial outputs: not selected - no runtime partial
  output behavior changes.
- Release / packaging / dependency compatibility: selected - removed placeholder
  packages must not be import/build/deploy dependencies.
- Documentation / migration notes: selected - docs and governance inventories
  are changed in the same PR.
- Geospatial / CRS / basin geometry: not selected - no GIS behavior change.
- Hydro-met time series / forcing windows: not selected - no forcing/time-series
  behavior change.
- SHUD numerical runtime / conservation / NaN: not selected - no solver runtime
  behavior change.
- PostGIS / TimescaleDB domain behavior: not selected - no database behavior
  change.
- Slurm production lifecycle / mock-vs-real parity: selected -
  `workers/sbatch_templates` retirement must preserve active `infra/sbatch` and
  gateway template defaults.
- External hydro-met providers / snapshot reproducibility: not selected - no
  provider snapshot behavior change.
- Run manifest / QC provenance: not selected - no run manifest/QC behavior
  change.
- Published NHMS artifacts / display identity: selected -
  `services/tile-publisher` cleanup must not affect active tile publication
  (`services/tile_publisher`) or display tile consumers.

Must preserve for #363:

- Active frontend build/test/deploy path remains `apps/frontend`.
- Active worker imports and console scripts remain underscore package paths:
  `workers/forcing_producer`, `workers/shud_runtime`,
  `workers/output_parser`, and `workers/flood_frequency`.
- Active tile publication remains `services/tile_publisher`,
  `infra/sbatch/publish_tiles.sbatch`, display tile API routes, and frontend
  tile consumers.
- Active Slurm templates remain `infra/sbatch` and
  `services/slurm_gateway/config.py` defaults continue to resolve there.
- #364, #365, and #366 surfaces are not changed.

Invariant Matrix for #363:

- Governing invariant: Retiring legacy placeholder paths must remove misleading
  active-looking entrypoints without changing any current runtime, import,
  build, deployment, Slurm, or display-tile contract.
- Source-of-truth identity/contract: path classification in
  `docs/governance/LEGACY_DEAD_CODE_INVENTORY.md` plus active counterparts in
  `docs/governance/ROLE_BOUNDARY.md`, `pyproject.toml`,
  `services/slurm_gateway/config.py`, `infra/sbatch`, and `apps/frontend`.
- Producers: legacy placeholder directories, archive docs, module index,
  project profile, role boundary, and legacy inventory.
- Validators/preflight: focused `rg`/`find`/`git ls-files` checks,
  `uv run ruff check .`, `uv run pytest -q tests/test_role_boundary_static.py
  tests/test_slurm_gateway_app.py tests/test_slurm_route_contract.py`, and
  affected frontend package commands if frontend tracked files change.
- Storage/cache/query: none - no DB, cache, object store, or query contract
  changes.
- Public routes/entrypoints: console scripts in `pyproject.toml`, API and
  frontend active entrypoints, `services/slurm_gateway` template settings, and
  `infra/sbatch` production templates.
- Frontend/downstream consumers: `apps/frontend`, `apps/api/routes/flood_alerts.py`,
  `services/tiles`, and frontend flood/map consumers remain unchanged unless
  docs reference them.
- Failure paths/rollback/stale state: deleted or archived legacy paths must not
  be referenced by active docs/config/tests after cleanup; historical references
  may remain only in archived or historical OpenSpec contexts.
- Evidence/audit/readiness: archive manifest for legacy Slurm templates, updated
  inventory final action/status, updated module index, and focused command
  outputs recorded in tasks/PR evidence.
- Regression rows:
  - `pyproject.toml` console scripts and active imports use underscore worker
    packages -> no reference to hyphenated worker placeholders is needed.
  - `services/slurm_gateway/config.py` default template root and tests use
    `infra/sbatch` -> no active dependency on `workers/sbatch_templates`.
  - `services/tile_publisher`, `infra/sbatch/publish_tiles.sbatch`, display API
    tile routes, and frontend tile consumers remain active -> no dependency on
    `services/tile-publisher`.
  - `apps/frontend` build/test metadata remains active -> no dependency on
    `apps/web`.
  - Broad current-doc reference scans after cleanup -> no current source-of-truth
    doc points readers to removed legacy paths as active entrypoints.
  - #364/#365/#366 governed paths -> unchanged behavior and ownership remain
    deferred to their issues.

Boundary-surface checklist for #363:

- Read surfaces: current docs, OpenSpec fixture, project profile, role boundary,
  legacy inventory, Slurm gateway config/tests, pyproject console scripts.
- Write/delete/archive surfaces: only `apps/web`, hyphenated worker placeholders,
  `services/tile-publisher`, `workers/sbatch_templates`, and docs/archive
  artifacts named by #363.
- Public entrypoints: do not remove `apps/frontend`, underscore worker packages,
  `services/tile_publisher`, `services/slurm_gateway`, or `infra/sbatch`.
- Evidence boundaries: archive docs must identify legacy status and active
  counterparts, not imply production readiness.
- Unchanged downstream consumers: QHH diagnostics, Playwright specs, CI paused
  job, active frontend, active API/display tile consumers, and production Slurm
  templates.

### D2. Do not delete diagnostic QHH scripts first

The QHH scripts already have `DIAGNOSTIC-ONLY` headers and static tests that keep them out of production orchestrator code. First create a diagnostic README/manifest and optionally move them behind short compatibility wrappers later.

Issue #364 applies the first diagnostic-isolation slice. It MUST NOT delete or
move QHH diagnostic scripts. It adds an explicit diagnostic manifest under
`scripts/diagnostic/qhh/` that names the current root-level diagnostic
entrypoints, their direct helper dependencies, out-of-chain QHH smoke helpers,
the production replacement commands, and the static guard tests that keep these
tokens out of production orchestrator code.

Fixture level for #364: `expanded`. The issue touches script-entry
classification, production-vs-diagnostic documentation, runbook guidance, and
static tests guarding production orchestrator boundaries.

Repair intensity for #364: `high`. The main risk is accidentally converting a
diagnostic/reproduction path into a production-looking entrypoint, or breaking a
diagnostic path that still has bring-up value.

Risk packs considered for #364:

- Public API / CLI / script entry: selected - the manifest classifies supported
  diagnostic script entrypoints and production replacement commands.
- Config / project setup: selected - production replacement must point to
  `infra/env/compute.example`, generic daemon, and `nhms-pipeline
  plan-production` rather than QHH-only scripts.
- File IO / path safety / overwrite: not selected - #364 does not add runtime
  file operations or move/delete scripts.
- Schema / columns / units / field names: not selected - no DB/API/schema
  contract changes.
- Auth / permissions / secrets: not selected - no auth or secret handling
  changes.
- Concurrency / shared state / ordering: selected - production scheduler and
  diagnostic continuous-loop ownership must remain separate.
- Resource limits / large input / discovery: selected - repository scans must be
  scoped to QHH diagnostic and orchestrator boundaries, not generated trees.
- Legacy compatibility / examples: selected - root-level diagnostic script
  paths remain available; any future move requires wrappers and runbook updates.
- Error handling / rollback / partial outputs: not selected - no runtime error
  or cleanup behavior changes.
- Release / packaging / dependency compatibility: selected - no console script,
  import package, or deployment entrypoint should switch to QHH diagnostics.
- Documentation / migration notes: selected - the manifest and runbook notes are
  the deliverable.
- Geospatial / CRS / basin geometry: not selected - no GIS behavior change.
- Hydro-met time series / forcing windows: not selected - no forcing/time-series
  behavior change.
- SHUD numerical runtime / conservation / NaN: not selected - no solver behavior
  change.
- PostGIS / TimescaleDB domain behavior: not selected - no database behavior
  change.
- Slurm production lifecycle / mock-vs-real parity: selected - the manifest must
  distinguish diagnostic QHH sbatch wrappers from gateway-owned production
  `infra/sbatch` templates.
- External hydro-met providers / snapshot reproducibility: not selected - no
  provider behavior change.
- Run manifest / QC provenance: selected - diagnostic manifest-builder and QHH
  smoke evidence artifacts must remain diagnostic-only and not substitute for
  production readiness.
- Published NHMS artifacts / display identity: selected - diagnostic display
  product publication must remain separate from production display-readiness
  proof.

Must preserve for #364:

- Existing root-level diagnostic paths remain callable:
  `scripts/run_qhh_continuous.py`, `scripts/run_qhh_cycle.sh`,
  `scripts/run_qhh_cycle.sbatch`, `scripts/run_qhh_backend_smoke.sh`, and
  `scripts/create_qhh_shud_manifest.py`.
- Direct helper dependencies recorded in the inventory remain root-level unless
  a later issue deliberately moves them with wrappers.
- Production scheduler/orchestrator code does not import, shell out to, or name
  QHH diagnostic tokens.
- `infra/sbatch` remains the production Slurm template ownership; QHH diagnostic
  sbatch is not a gateway template.
- #365 Playwright/e2e and #366 CI paused-job surfaces are not changed.

Invariant Matrix for #364:

- Governing invariant: QHH diagnostic scripts remain visible and usable as
  diagnostic/reproduction assets, while production scheduler/orchestrator paths
  stay generic and cannot depend on QHH diagnostic entrypoints.
- Source-of-truth identity/contract: diagnostic manifest under
  `scripts/diagnostic/qhh/`, `DIAGNOSTIC-ONLY` script headers,
  `docs/governance/LEGACY_DEAD_CODE_INVENTORY.md`, static guard tests in
  `tests/test_qhh_scripts_static.py`, and production replacement commands in
  `infra/env/compute.example` / orchestrator docs.
- Producers: diagnostic manifest and any runbook/governance doc updates.
- Validators/preflight: `tests/test_qhh_scripts_static.py`, focused `rg` scans
  over `services/orchestrator`, `scripts`, `docs/runbooks`, and governance docs,
  plus docs lint and ruff.
- Storage/cache/query: none - no DB/object-store/cache/query changes.
- Public routes/entrypoints: root-level QHH diagnostic scripts stay stable;
  generic production entrypoints remain `nhms-pipeline plan-production` and
  orchestrator scheduler commands.
- Frontend/downstream consumers: none - no frontend or display API code changes.
- Failure paths/rollback/stale state: future moves require wrappers; this slice
  should leave no moved-path runbook breakage.
- Evidence/audit/readiness: manifest entries for diagnostic entrypoints, helper
  dependencies, out-of-chain helper note, production replacement, and guard
  tests.
- Regression rows:
  - `tests/test_qhh_scripts_static.py` -> passes and confirms production
    orchestrator sources do not reference QHH diagnostic tokens.
  - `rg` over `services/orchestrator/**/*.py` for QHH diagnostic tokens -> no
    production references.
  - Root diagnostic scripts -> remain tracked at their current paths.
  - Manifest -> lists backend-smoke, continuous/cycle, sbatch wrapper,
    manifest-builder, helper dependencies, out-of-chain helper, production
    replacement, and guard tests.
  - #365/#366 surfaces -> unchanged.

Boundary-surface checklist for #364:

- Public script surfaces: root `scripts/run_qhh_*`, backend-smoke, and
  diagnostic manifest builder.
- Shared production surfaces: `services/orchestrator/**`, `infra/sbatch`,
  `infra/env/compute.example`, and `nhms-pipeline plan-production`.
- Evidence boundaries: QHH smoke summaries/display products must be described as
  diagnostic evidence, not production live readiness.
- Unchanged downstream consumers: frontend e2e, CI workflows, placeholder
  retired paths from #363, production scheduler implementation.

### D3. Delete or archive placeholders only with import/CI/runbook evidence

Empty or README-only placeholders can be deleted or archived after grep/import/CI evidence proves no active path depends on them. `workers/sbatch_templates` needs a stronger migration note because it contains actual sbatch templates.

### D4. Separate mocked regression from live proof

Mocked Playwright tests remain useful, but filenames/config/docs must say mocked regression. Live e2e profiles must prohibit `page.route('**/api/v1/**')`.

The live display-readonly profile is a deliverable, not only a plan. If live credentials or node access are unavailable, the issue may mark runtime execution blocked, but the config/script/static no-mock guard still needs to land.

Issue #365 applies this split. It should prefer a low-churn classification
surface over renaming every existing Playwright file: existing broad API-mocked
specs may remain in `apps/frontend/e2e/` if the default/local config and docs
classify them as mocked regression, while the new live profile owns a distinct
file pattern/config/script and cannot run specs that register broad API mocks.

Fixture level for #365: `expanded`. The issue touches frontend test
configuration, package scripts, evidence semantics, validation docs, and bug
ledger status.

Repair intensity for #365: `high`. The main risk is evidence laundering:
deterministic mocked browser tests are useful, but they must not be cited as
node-27/live display proof. A second risk is introducing a live Playwright
profile that silently falls back to local mocked APIs or placeholder base URLs.

Risk packs considered for #365:

- Public API / CLI / script entry: selected - package scripts and Playwright
  configs are user-facing validation entrypoints.
- Config / project setup: selected - the live profile must require explicit
  base URL and API base URL inputs.
- File IO / path safety / overwrite: not selected - no artifact write or file
  movement beyond frontend test/config/docs is expected.
- Schema / columns / units / field names: not selected - no API or DB schema
  changes.
- Auth / permissions / secrets: selected - live URL docs must not hard-code
  secrets, private credentials, or write-capable control-plane actions.
- Concurrency / shared state / ordering: not selected - no shared scheduler or
  runtime state changes.
- Resource limits / large input / discovery: selected - static guards should
  scan bounded frontend e2e/config files and avoid generated directories.
- Legacy compatibility / examples: selected - existing mocked specs should
  remain runnable under a clearly named mocked-regression lane.
- Error handling / rollback / partial outputs: selected - missing live runtime
  is `BLOCKED`, not `PASS`.
- Release / packaging / dependency compatibility: selected - pnpm scripts and
  Playwright configs must work with the existing frontend toolchain.
- Documentation / migration notes: selected - `docs/VALIDATION.md` and
  `docs/bugs.md` must say mocked specs are not live receipts.
- Geospatial / CRS / basin geometry: not selected - no GIS implementation
  changes.
- Hydro-met time series / forcing windows: not selected - no data semantics
  changes.
- SHUD numerical runtime / conservation / NaN: not selected - no solver
  changes.
- PostGIS / TimescaleDB domain behavior: not selected - no database behavior
  changes.
- Slurm production lifecycle / mock-vs-real parity: selected - live display
  proof must not be replaced by deterministic mocked UI tests.
- External hydro-met providers / snapshot reproducibility: not selected - no
  provider snapshot behavior changes.
- Run manifest / QC provenance: selected - browser evidence status must be
  bound to live-vs-mocked provenance.
- Published NHMS artifacts / display identity: selected - live display profile
  must target display_readonly API/frontend surfaces and not mocked artifacts.

Must preserve for #365:

- Existing deterministic Playwright specs remain available as frontend
  regression coverage.
- Broad `page.route('**/api/v1/**')` mocks are allowed only in files/lane names
  classified as mocked regression, preview, or visual evidence.
- The live display_readonly lane has its own config/script and refuses broad API
  route mocks.
- The live profile requires explicit frontend base URL and API base URL; it must
  not default to `https://api.example.test` or a local dev server with mocked
  API data.
- A live PASS requires browser-observed runtime config and read API responses
  from the configured live API binding; request-context-only checks, RBAC
  denied pages, runtime-config unavailable pages, Slurm browser requests, and
  retry/cancel mutations are not live receipts.
- Runtime unavailable is recorded as `BLOCKED` with required variables, not as a
  passing live receipt.

Invariant Matrix for #365:

- Governing invariant: mocked frontend Playwright regression and live
  display_readonly evidence are separate evidence classes; a spec that broadly
  mocks `/api/v1` cannot satisfy live display receipt.
- Source-of-truth identity/contract: Playwright config/script names, live spec
  file pattern, static no-mock guard, `docs/VALIDATION.md`, `docs/bugs.md`, and
  `docs/runbooks/two-node-production-e2e-plan.md`.
- Producers: frontend Playwright configs, package scripts, live display spec or
  profile, mocked-regression grouping, and docs updates.
- Validators/preflight: frontend unit tests, focused e2e/static guard, bounded
  `rg` scans over `apps/frontend/e2e`, and docs lint.
- Storage/cache/query: none - no DB/cache/query implementation changes.
- Public routes/entrypoints: `corepack pnpm test`, existing mocked-regression
  e2e lane, new live display_readonly e2e lane.
- Frontend/downstream consumers: existing Playwright specs and validation docs.
- Failure paths/rollback/stale state: missing live runtime records `BLOCKED`
  with required env vars and does not mutate the default mocked lane.
- Evidence/audit/readiness: docs and test output distinguish mocked regression,
  live blocked, and live pass.
- Regression rows:
  - Existing mocked-regression lane -> still runs deterministic specs that mock
    `/api/v1`.
  - Live display_readonly static guard -> fails if a live e2e file registers
    `page.route('**/api/v1/**')`.
  - Live config without required base URL/API base URL -> fails clearly before
    browser execution.
  - Live browser/API binding -> page responses include display_readonly runtime
    config and monitoring read API responses from either the configured distinct
    API origin or documented same-origin proxy.
  - Live display control guard -> any `/api/v1/slurm/*` browser request is
    forbidden, while retry/cancel checks remain mutation-sensitive.
  - Live runtime unavailable during implementation -> documented as `BLOCKED`
    with required variables, not `PASS`.
  - `docs/VALIDATION.md` / `docs/bugs.md` -> no longer cite mocked Playwright
    specs as live receipt.

Boundary-surface checklist for #365:

- Frontend config surfaces: `apps/frontend/playwright*.config.ts`,
  `apps/frontend/package.json`, and config helper tests.
- Frontend e2e surfaces: existing broad-mock specs, any new live spec pattern,
  and preview/visual evidence lanes that may intentionally mock APIs.
- Evidence docs: `docs/VALIDATION.md`, `docs/bugs.md`, and two-node e2e runbook
  compatibility.
- Unchanged downstream consumers: API/backend routes, scheduler, Slurm gateway,
  QHH diagnostic scripts, and #366 CI paused-job cleanup.

## Risks / Mitigations

- **Risk: removing historical placeholders breaks old docs.** Mitigation: update docs or archive historical docs in the same PR.
- **Risk: moving diagnostic scripts breaks runbooks.** Mitigation: first add README/manifest; move only with wrappers and deprecation messaging.
- **Risk: live e2e cannot run locally.** Mitigation: separate config/profile and make live receipt explicit, not part of fast local tests.

## Verification

For issue #362, verification proves inventory completeness and formatting only:

- `rg` inventory for candidate paths records discovered references and active
  counterparts; the expected result is a completed inventory, not deletion.
- `find` inventory records governed candidate directories and active
  counterparts.
- `uv run ruff check .` exits 0.
- `npx --yes markdownlint-cli2 --config .markdownlint.yaml 'docs/**/*.md'`
  exits 0.
- Focused workflow grep records any `&& false` paused job in the inventory; #362
  does not remove it.

Follow-up #363-#366 verification adds runtime/static checks such as
`tests/test_qhh_scripts_static.py`, frontend build/tests, and workflow cleanup
proof when those issues change the relevant files.
