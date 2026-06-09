## 0. Dependency gate

- [x] 0.1 Confirm `governance-0-ci-contract-baseline` is merged and green, or
  record an explicit maintainer waiver listing current red checks. Evidence
  recorded 2026-06-09: issue #358 closed 2026-06-08 17:33:44Z via merged PR
  #375 (`fix(contract): reconcile generated frontend API types`, merged
  2026-06-08 17:33:43Z); issue #359 closed 2026-06-08 18:21:30Z via merged PR
  #376 (`chore(tooling): run Makefile Python targets via uv`, merged
  2026-06-08 18:21:28Z); parent baseline issue #353 closed 2026-06-08
  18:22:19Z before #362 inventory completion.

## 1. Inventory and classification

- [x] 1.1 Create a persistent legacy path inventory covering `apps/web`, hyphenated worker placeholders, `workers/sbatch_templates`, `services/tile-publisher`, QHH diagnostic scripts and direct helper dependencies, mocked e2e specs, and paused CI jobs. Evidence: `docs/governance/LEGACY_DEAD_CODE_INVENTORY.md`.
- [x] 1.2 For each inventory row, record exact path, status, owner area, active build/import/deploy evidence, docs/runbook migration, final action, and verification command. Evidence: governed inventory and active counterpart tables.
- [x] 1.3 For issue #362, keep the PR inventory-only: do not delete, move, rename, archive, or wrap any governed path. Evidence: inventory scope and follow-up ownership sections keep #363-#366 actions separate.
- [x] 1.4 Classify owner area with the four-role vocabulary from `docs/governance/ROLE_BOUNDARY.md`: `compute_control`, `display_readonly`, `slurm_gateway`, or `shared_contract`. Evidence: every inventory row uses the role-boundary vocabulary.
- [x] 1.5 Record discovery commands and results precisely enough that #363-#366 can proceed without redoing broad discovery. Evidence: discovery command register `D1`-`D10` and required discovery results.
- [x] 1.6 Required #362 evidence:
  - Input command:

    ```bash
    pattern="apps/web|workers/(forcing-producer|shud-runtime|output-parser|flood-frequency|sbatch_templates)"
    pattern="${pattern}|services/tile-publisher|services/tile_publisher|infra/sbatch"
    pattern="${pattern}|SLURM_GATEWAY_TEMPLATE_DIR|template_dir"
    pattern="${pattern}|run_qhh_continuous|run_qhh_cycle|run_qhh_backend_smoke|create_qhh_shud_manifest"
    pattern="${pattern}|frontend-m15-visual|&& false|page\\.route\\('.*api/v1"
    rg -n \
      --glob '!apps/frontend/node_modules/**' \
      --glob '!apps/frontend/dist/**' \
      --glob '!**/__pycache__/**' \
      "$pattern" \
      .
    ```

    Expected output: references needed to classify the governed paths plus
    active counterparts such as `apps/frontend`, underscore worker packages,
    `infra/sbatch`, Slurm gateway template settings, and tile publisher/display
    implementation. Each relevant hit is reflected or summarized in the
    inventory evidence column.
  - Input command:
    `find apps workers services scripts .github/workflows -maxdepth 3 \( -path apps/frontend/node_modules -o -path apps/frontend/dist -o -path '*/__pycache__' \) -prune -o -type d -print | sort`
    Expected output: governed candidate directories and active counterpart
    directories are visible and reflected in the inventory; generated directories
    such as `apps/frontend/node_modules`, `apps/frontend/dist`, and `__pycache__`
    subtrees are intentionally excluded.
  - Input command: `uv run ruff check .`
    Expected output: exit 0.
  - Input command:

    ```bash
    npx --yes markdownlint-cli2 --config .markdownlint.yaml \
      docs/governance/LEGACY_DEAD_CODE_INVENTORY.md \
      docs/runbooks/qhh-continuous.md \
      docs/runbooks/qhh-22-business-bringup.md \
      openspec/changes/governance-2-legacy-dead-code-retirement/design.md \
      openspec/changes/governance-2-legacy-dead-code-retirement/specs/legacy-dead-code-retirement/spec.md \
      openspec/changes/governance-2-legacy-dead-code-retirement/tasks.md \
      scripts/diagnostic/qhh/README.md
    ```

    Expected output: exit 0.
  - Input command:
    `rg -n "seed_qhh|reset_qhh|summarize_qhh|publish_qhh|apply_smoke_migrations|create_qhh_shud_manifest|run_qhh_backend_smoke|run_qhh_cycle" scripts docs/runbooks tests docs/governance --glob '!**/__pycache__/**'`
    Expected output: QHH entrypoints, direct helper dependencies, runbook
    evidence surfaces, static test surfaces, and the related out-of-chain
    `scripts/seed_qhh_smoke_met_station.py` classification are reflected in
    `docs/governance/LEGACY_DEAD_CODE_INVENTORY.md`.
- [x] 1.7 #362 non-goals:
  - No placeholder deletion/archive.
  - No QHH script move/wrapper.
  - No Playwright spec rename or live e2e config.
  - No CI workflow behavior change.
  - No cleanup approval for #363-#366 beyond recorded evidence and proposed final action.

## 2. Placeholder cleanup (#363 follow-up, not #362)

- [x] 2.1 Remove or archive `apps/web` after updating current docs that mention
  it. Required evidence:
  - Input command:
    `git ls-files apps/web apps/frontend | sort`
    Expected output: `apps/web` files are absent after cleanup, while
    `apps/frontend` tracked files remain.
  - Input command:
    `rg -n "apps/web" README.md docs openspec/project-profile.md openspec/changes/governance-2-legacy-dead-code-retirement --glob '!docs/archived/**'`
    Expected output: no current-source-of-truth doc presents `apps/web` as an
    active entrypoint; any remaining mention is in this change, the legacy
    inventory, or explicit historical/archive context.
- [x] 2.2 Remove or archive hyphenated worker placeholder directories after
  proving canonical underscore packages are the only active entrypoints.
  Required evidence:
  - Input command:
    `git ls-files workers/forcing-producer workers/shud-runtime workers/output-parser workers/flood-frequency workers/forcing_producer workers/shud_runtime workers/output_parser workers/flood_frequency | sort`
    Expected output: hyphenated placeholder files are absent after cleanup,
    while underscore worker package files remain.
  - Input command:
    `rg -n "workers/(forcing-producer|shud-runtime|output-parser|flood-frequency)" README.md docs openspec/project-profile.md pyproject.toml services workers tests scripts --glob '!docs/archived/**' --glob '!**/__pycache__/**'`
    Expected output: no current active reference depends on hyphenated worker
    paths; remaining mentions are explicit historical/governance references.
  - Input command:
    `sed -n '35,55p' pyproject.toml`
    Expected output: console scripts point to underscore worker packages.
- [x] 2.3 Remove or archive `services/tile-publisher` after proving active tile
  publication and display paths use `services/tile_publisher`, `infra/sbatch`,
  API tile routes, and frontend consumers. Required evidence:
  - Input command:
    `git ls-files services/tile-publisher services/tile_publisher services/tiles infra/sbatch/publish_tiles.sbatch apps/api/routes/flood_alerts.py | sort`
    Expected output: hyphenated placeholder files are absent after cleanup,
    while active tile publisher, tile service, sbatch, and API route files
    remain.
  - Input command:

    ```bash
    pattern="services/tile-publisher|services\\.tile_publisher|services/tile_publisher|publish_tiles|api/v1/tiles"
    rg -n "$pattern" \
      services apps infra tests docs openspec/project-profile.md \
      openspec/changes/governance-2-legacy-dead-code-retirement \
      --glob '!docs/archived/**' \
      --glob '!**/__pycache__/**' \
      --glob '!apps/frontend/node_modules/**' \
      --glob '!apps/frontend/dist/**'
    ```

    Expected output: current active references use `services.tile_publisher`,
    `services/tile_publisher`, `infra/sbatch/publish_tiles.sbatch`, or display
    tile routes; `services/tile-publisher` appears only as retired/historical
    governance context.
- [x] 2.4 Treat `workers/sbatch_templates` separately: preserve a legacy/archive
  manifest before deleting or archiving the active-tree directory. Required
  evidence:
  - Input command:
    `git ls-files docs/archived/legacy-slurm-templates.md workers/sbatch_templates infra/sbatch | sort`
    Expected output: the legacy archive doc and active `infra/sbatch` templates
    are tracked; no `workers/sbatch_templates` tracked files remain.
  - Input command:
    `test ! -e workers/sbatch_templates && printf '%s\n' 'workers/sbatch_templates absent'`
    Expected output: `workers/sbatch_templates absent`.
  - Input command:
    `rg -n "workers/sbatch_templates|infra/sbatch|SLURM_GATEWAY_TEMPLATE_DIR|template_dir|DEFAULT_JOB_TYPE_TEMPLATES" services/slurm_gateway infra tests docs openspec/project-profile.md openspec/changes/governance-2-legacy-dead-code-retirement --glob '!docs/archived/**' --glob '!**/__pycache__/**'`
    Expected output: active config/tests/docs point to `infra/sbatch`; remaining
    `workers/sbatch_templates` mentions are explicit legacy/governance context.
- [x] 2.5 Update current governance docs and module index so new contributors
  can identify active versus retired paths without opening historical OpenSpec
  records. Required evidence:
  - Input command:

    ```bash
    npx --yes markdownlint-cli2 --config .markdownlint.yaml \
      docs/governance/LEGACY_DEAD_CODE_INVENTORY.md \
      docs/runbooks/qhh-continuous.md \
      docs/runbooks/qhh-22-business-bringup.md \
      openspec/changes/governance-2-legacy-dead-code-retirement/design.md \
      openspec/changes/governance-2-legacy-dead-code-retirement/specs/legacy-dead-code-retirement/spec.md \
      openspec/changes/governance-2-legacy-dead-code-retirement/tasks.md \
      scripts/diagnostic/qhh/README.md
    ```

    Expected output: exit 0.
  - Input command:
    `uv run ruff check .`
    Expected output: exit 0.
  - Input command:
    `uv run pytest -q tests/test_role_boundary_static.py tests/test_slurm_gateway_app.py tests/test_slurm_route_contract.py`
    Expected output: exit 0.
- [x] 2.6 Confirm #363 did not modify deferred #364-#366 surfaces. Required
  evidence:
  - Input command:
    `git diff --name-only origin/master...HEAD`
    Expected output: no changes under `scripts/run_qhh*`,
    `scripts/create_qhh_shud_manifest.py`, `apps/frontend/e2e`, or
    `.github/workflows/ci.yml`.

## 3. Diagnostic isolation (#364 follow-up, not #362)

- [x] 3.1 Add `scripts/diagnostic/qhh/README.md` or an equivalent diagnostic
  manifest. Required manifest content:
  - QHH diagnostic entrypoints:
    `scripts/run_qhh_continuous.py`, `scripts/run_qhh_cycle.sh`,
    `scripts/run_qhh_cycle.sbatch`, `scripts/run_qhh_backend_smoke.sh`, and
    `scripts/create_qhh_shud_manifest.py`.
  - Direct helper dependencies:
    `scripts/apply_smoke_migrations.py`, `scripts/reset_qhh_smoke_db.py`,
    `scripts/seed_qhh_forcing_stations.py`,
    `scripts/seed_qhh_shud_output_segments.py`,
    `scripts/summarize_qhh_smoke_results.py`, and
    `scripts/publish_qhh_display_products.py`.
  - Out-of-chain helper note for `scripts/seed_qhh_smoke_met_station.py`.
  - Production replacement: generic production scheduler/daemon path, not QHH
    scripts.
  - Static guard tests that enforce production isolation.
  Required evidence:
  - Input command:
    `test -f scripts/diagnostic/qhh/README.md && sed -n '1,220p' scripts/diagnostic/qhh/README.md`
    Expected output: manifest names the entrypoints, helpers, out-of-chain
    helper, production replacement, and guard tests above.
- [x] 3.2 Preserve current root-level diagnostic paths unless this issue moves
  them with wrappers. Required evidence:
  - Input command:

    ```bash
    git ls-files \
      scripts/run_qhh_continuous.py \
      scripts/run_qhh_cycle.sh \
      scripts/run_qhh_cycle.sbatch \
      scripts/run_qhh_backend_smoke.sh \
      scripts/create_qhh_shud_manifest.py \
      scripts/apply_smoke_migrations.py \
      scripts/reset_qhh_smoke_db.py \
      scripts/seed_qhh_forcing_stations.py \
      scripts/seed_qhh_shud_output_segments.py \
      scripts/summarize_qhh_smoke_results.py \
      scripts/publish_qhh_display_products.py \
      scripts/seed_qhh_smoke_met_station.py \
      | sort
    ```

    Expected output: existing root-level QHH diagnostic entrypoints and helper
    files remain tracked.
- [x] 3.3 Keep or strengthen production-orchestrator static guards. Required
  evidence:
  - Input command:
    `uv run pytest -q tests/test_qhh_scripts_static.py`
    Expected output: exit 0.
  - Input command:
    Explicit negative assertion:

    ```bash
    if rg -n "run_qhh_continuous|run_qhh_cycle|run_qhh_backend_smoke|create_qhh_shud_manifest" \
      services/orchestrator \
      --glob '*.py'
    then
      printf '%s\n' 'unexpected QHH diagnostic token in services/orchestrator'
      exit 1
    else
      status=$?
      if [ "$status" -eq 1 ]; then
        printf '%s\n' 'no QHH diagnostic tokens in services/orchestrator (rg exit 1)'
      else
        printf '%s\n' "rg failed unexpectedly with exit $status"
        exit "$status"
      fi
    fi
    ```

    Expected output: `no QHH diagnostic tokens in services/orchestrator (rg exit
    1)` and exit 0; raw `rg` exit 1 is the passing no-match condition.
  - Input command:

    ```bash
    pattern="DIAGNOSTIC-ONLY|run_qhh_continuous|run_qhh_cycle|run_qhh_backend_smoke|create_qhh_shud_manifest"
    pattern="${pattern}|seed_qhh_forcing_stations|seed_qhh_shud_output_segments"
    pattern="${pattern}|summarize_qhh_smoke_results|publish_qhh_display_products|seed_qhh_smoke_met_station"
    rg -n "$pattern" \
      scripts docs/runbooks docs/governance tests/test_qhh_scripts_static.py \
      scripts/diagnostic/qhh \
      --glob '!**/__pycache__/**'
    ```

    Expected output: diagnostic entrypoints, helper dependencies, runbook
    evidence, inventory, static tests, and the new manifest are visible; no
    production orchestrator source is part of this result set.
- [x] 3.4 Update runbook/governance docs only if needed to point at the
  diagnostic manifest and preserve production replacement wording. Required
  evidence:
  - Input command:
    `rg -n "run_qhh_continuous|run_qhh_cycle|run_qhh_backend_smoke|create_qhh_shud_manifest|diagnostic/qhh|plan-production|generic production scheduler|DIAGNOSTIC-ONLY" docs/runbooks docs/governance infra/env/compute.example scripts/diagnostic/qhh`
    Expected output: QHH diagnostic runbooks remain diagnostic/reproduction
    guidance, and production replacement wording points to generic production
    scheduler or `nhms-pipeline plan-production`.
  - Input command:
    `uv run ruff check .`
    Expected output: exit 0.
  - Input command:

    ```bash
    npx --yes markdownlint-cli2 --config .markdownlint.yaml \
      docs/governance/LEGACY_DEAD_CODE_INVENTORY.md \
      docs/runbooks/qhh-continuous.md \
      docs/runbooks/qhh-22-business-bringup.md \
      openspec/changes/governance-2-legacy-dead-code-retirement/design.md \
      openspec/changes/governance-2-legacy-dead-code-retirement/specs/legacy-dead-code-retirement/spec.md \
      openspec/changes/governance-2-legacy-dead-code-retirement/tasks.md \
      scripts/diagnostic/qhh/README.md
    ```

    Expected output: exit 0.
- [x] 3.5 Confirm #364 did not modify deferred #365-#366 surfaces or move
  diagnostic scripts without wrappers. Required evidence:
  - Input command:
    `git diff --name-only origin/master...HEAD`
    Expected output: changes are limited to the QHH diagnostic manifest,
    OpenSpec fixture, and directly relevant QHH runbook/governance docs/tests;
    no changes under `apps/frontend/e2e` or `.github/workflows/ci.yml`.

## 4. E2E evidence split (#365 follow-up, not #362)

- [x] 4.1 Rename mocked Playwright specs or add config grouping so API-mocked
  specs are visibly `mocked-regression`. Required evidence:
  - Input command:

    ```bash
    cd apps/frontend
    corepack pnpm exec playwright test --list
    ```

    Expected output: existing deterministic specs remain discoverable under a
    mocked-regression/default lane and are not named or documented as live
    display receipt.
  - Input command:

    ```bash
    rg -n "page\\.route\\('\\*\\*/api/v1/\\*\\*'" apps/frontend/e2e
    ```

    Expected output: every broad API mock hit belongs to mocked-regression,
    preview, or visual-evidence files/lane documentation; no live-display spec
    appears in this result.
  - Evidence recorded 2026-06-09: `corepack pnpm exec playwright test --list`
    listed 82 tests under project `mocked-regression-chromium`; raw broad mock
    `rg` hits were limited to existing mocked-regression, preview, and visual
    specs, with no `live-display.spec.ts` hit. Phase 6 verification additionally
    confirmed `corepack pnpm exec playwright test --project=chromium --list`
    exits non-zero with available project `mocked-regression-chromium`.
- [x] 4.1.1 Phase 6 fix: remove the generic `chromium` project alias so an
  explicit `--project=chromium` caller cannot list broad-mock specs as generic
  Chromium evidence. Required evidence:
  - Input command:

    ```bash
    cd apps/frontend
    corepack pnpm exec playwright test e2e/m15-visual-conformance.spec.ts --project=mocked-regression-chromium --list
    ```

    Expected output: M15 visual conformance tests list under
    `mocked-regression-chromium`; `--project=chromium` is not a supported
    compatibility alias.
  - Evidence recorded 2026-06-09: command listed 41 M15 visual conformance
    tests under `mocked-regression-chromium`.
- [x] 4.2 Add a live display-readonly e2e profile and npm/pnpm script that uses
  explicit frontend base URL and API base URL, and forbids broad
  `page.route('**/api/v1/**')` mocks. Required evidence:
  - Input command:

    ```bash
    cd apps/frontend
    corepack pnpm run test:e2e:live-display -- --list
    ```

    Expected output: without required live runtime URL variables, the command
    exits non-zero before browser execution with a clear message naming the
    required variables; record this as `BLOCKED`, not `PASS`.
  - Input command:

    ```bash
    rg -n "test:e2e:live-display|PLAYWRIGHT_LIVE_BASE_URL|PLAYWRIGHT_LIVE_API_BASE_URL|VITE_API_BASE_URL" \
      apps/frontend/package.json apps/frontend/playwright*.config.ts docs/VALIDATION.md docs/bugs.md
    ```

    Expected output: live script/config/docs require explicit live frontend and
    API URLs and do not fall back to `https://api.example.test`.
  - Evidence recorded 2026-06-09: `corepack pnpm run
    test:e2e:live-display -- --list` exited 1 before browser execution with
    `Live display Playwright profile BLOCKED: missing
    PLAYWRIGHT_LIVE_BASE_URL, PLAYWRIGHT_LIVE_API_BASE_URL`; with
    `PLAYWRIGHT_LIVE_BASE_URL=http://127.0.0.1:4174` and
    `PLAYWRIGHT_LIVE_API_BASE_URL=http://127.0.0.1:8000`, `--list` showed the
    single `live-display-readonly-chromium` spec. Config/script/docs require
    explicit live env vars and the live config maps the API URL to
    `VITE_API_BASE_URL`.
- [x] 4.2.1 Phase 6 fix: tighten live display PASS criteria so the browser page
  itself must observe display_readonly runtime config and a monitoring read API
  from the configured live API binding. Required evidence:
  - Input command:

    ```bash
    cd apps/frontend
    corepack pnpm test src/__tests__/playwrightConfig.test.ts
    ```

    Expected output: helper tests cover distinct API and same-origin proxy
    binding, browser-observed runtime/read evidence, RBAC-denied and runtime
    unavailable non-PASS states, and forbidden control request classification.
  - Runtime acceptance: `corepack pnpm run test:e2e:live-display` may record
    `BLOCKED` when live runtime is unavailable, but a PASS requires browser
    responses from the configured API binding rather than Playwright request
    context alone.
  - Evidence recorded 2026-06-09: `corepack pnpm test
    src/__tests__/playwrightConfig.test.ts` passed 11 tests covering the live
    API binding and non-PASS classifications.
- [x] 4.2.2 Phase 6.2 invariant closure: close live evidence-boundary drift.
  Required evidence:
  - The live Playwright `testMatch` and static no-mock guard share the same
    exact live spec predicate; `xlive-display.spec.ts` is covered as a
    near-match regression and cannot be executed by the live profile without
    the same guard semantics.
  - `PLAYWRIGHT_LIVE_BASE_URL` and `PLAYWRIGHT_LIVE_API_BASE_URL` reject
    username/password URL userinfo before browser execution.
  - Live PASS requires runtime config `service_role` exactly
    `display_readonly`; `display_readonly: true` cannot override another
    service role.
  - Browser evidence parses only bounded runtime config JSON and records
    monitoring read API evidence from URL/status without parsing response
    bodies.
- [x] 4.3 Add a static guard that fails if files classified as live e2e contain
  broad API route mocks. Required evidence:
  - Input command:

    ```bash
    cd apps/frontend
    corepack pnpm test src/__tests__/playwrightConfig.test.ts
    ```

    Expected output: exit 0, including live-profile no-mock guard tests.
  - Input command:

    ```bash
    rg -n "page\\.route\\('\\*\\*/api/v1/\\*\\*'" apps/frontend/e2e --glob '*live*'
    ```

    Expected output: no matches; raw `rg` exit 1 is the passing no-match
    condition for live e2e files.
  - Evidence recorded 2026-06-09: `corepack pnpm test
    src/__tests__/playwrightConfig.test.ts` passed 11 tests, including live env
    fail-fast, live no-mock guard coverage, symlink-safe discovery, browser/API
    binding, and forbidden request classification; `rg -n
    "page\\.route\\('\\*\\*/api/v1/\\*\\*'" apps/frontend/e2e --glob
    '*live*'` exited 1 with no matches.
- [x] 4.3.1 Phase 6 fix: bound live spec discovery to intended e2e files and
  skip symlink traversal, generated directories, outside-root symlinks, and
  symlink cycles. Required evidence:
  - Input command:

    ```bash
    cd apps/frontend
    corepack pnpm test src/__tests__/playwrightConfig.test.ts
    ```

    Expected output: guard tests include outside symlink and symlink
    cycle/self-reference coverage.
  - Evidence recorded 2026-06-09: focused config helper test passed 11 tests,
    including outside symlink and symlink cycle/self-reference cases.
- [x] 4.4 If live runtime is unavailable, record runtime execution as `BLOCKED`
  while still landing config/script/static guard. Required evidence:
  - Input command:

    ```bash
    rg -n "BLOCKED|PLAYWRIGHT_LIVE_BASE_URL|PLAYWRIGHT_LIVE_API_BASE_URL|mocked regression|live display" \
      docs/VALIDATION.md docs/bugs.md apps/frontend/playwright*.config.ts
    ```

    Expected output: docs specify required variables and say unavailable live
    runtime is `BLOCKED`, not `PASS`.
  - Evidence recorded 2026-06-09: `docs/VALIDATION.md`, `docs/bugs.md`, and
    `apps/frontend/playwright.live-display.config.ts` name
    `PLAYWRIGHT_LIVE_BASE_URL` and `PLAYWRIGHT_LIVE_API_BASE_URL`; docs record
    missing live runtime as `BLOCKED`, not `PASS`.
- [x] 4.5 Update `docs/VALIDATION.md` and `docs/bugs.md` so mocked regression
  cannot be cited as live receipt. Required evidence:
  - Input command:

    ```bash
    rg -n "mocked regression|live receipt|page\\.route\\('\\*\\*/api/v1/\\*\\*'|test:e2e:live-display" \
      docs/VALIDATION.md docs/bugs.md
    ```

    Expected output: validation and bug docs distinguish mocked regression,
    blocked live runtime, and live receipt.
  - Input command:

    ```bash
    cd apps/frontend
    corepack pnpm test
    corepack pnpm build
    ```

    Expected output: exit 0.
  - Evidence recorded 2026-06-09: docs distinguish mocked regression, live
    receipt, and blocked live runtime. Full frontend `test` and `build`
    verification is part of this issue closeout command set.

## 5. Paused CI cleanup (#366 follow-up, not #362)

- [ ] 5.1 Replace the `frontend-m15-visual` `&& false` job with archived documentation or a manual workflow.
- [ ] 5.2 Verify workflow files no longer contain indefinite `&& false` disabled jobs.
