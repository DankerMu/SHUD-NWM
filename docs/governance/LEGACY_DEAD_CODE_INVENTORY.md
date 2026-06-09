# Legacy Dead-Code Inventory

Generated: 2026-06-09

Scope: Governance-2A issue #362. This artifact is inventory-only. It does not delete, move, rename, archive, wrap, or change runtime behavior for any governed path.

Proposed final actions below are not cleanup approval. Later issues own the destructive or behavioral changes: #363 owns placeholder deletion/archive decisions, #364 owns QHH diagnostic isolation, #365 owns mocked-vs-live Playwright separation, and #366 owns paused CI retirement.

## Status Vocabulary

Status values are limited to the OpenSpec vocabulary:

- `production`: active runtime, build, import, or deploy path.
- `diagnostic`: supported bring-up, debug, evidence, or reproduction path that is not production automation.
- `test-only`: regression fixture, mocked test, or CI test lane.
- `archived`: historical reference or placeholder outside active build, test, import, and deploy paths.

Owner areas use the role vocabulary from `docs/governance/ROLE_BOUNDARY.md`: `compute_control`, `display_readonly`, `slurm_gateway`, or `shared_contract`.

## Discovery Command Register

Rows below cite these command IDs in `verification_command`.

`D1` required broad reference inventory:

```bash
rg -n --glob '!apps/frontend/node_modules/**' --glob '!apps/frontend/dist/**' --glob '!**/__pycache__/**' "apps/web|workers/(forcing-producer|shud-runtime|output-parser|flood-frequency|sbatch_templates)|services/tile-publisher|services/tile_publisher|infra/sbatch|SLURM_GATEWAY_TEMPLATE_DIR|template_dir|run_qhh_continuous|run_qhh_cycle|run_qhh_backend_smoke|create_qhh_shud_manifest|frontend-m15-visual|&& false|page\\.route\\('.*api/v1" .
```

`D2` required directory inventory:

```bash
find apps workers services scripts .github/workflows -maxdepth 3 \( -path apps/frontend/node_modules -o -path apps/frontend/dist -o -path '*/__pycache__' \) -prune -o -type d -print | sort
```

`D3` placeholder file inventory:

```bash
find apps/web workers/forcing-producer workers/shud-runtime workers/output-parser workers/flood-frequency services/tile-publisher -maxdepth 2 -type f | sort
```

`D4` mocked Playwright broad-route inventory:

```bash
rg -n "page\\.route\\('.*api/v1" apps/frontend/e2e && rg -c "page\\.route\\('.*api/v1" apps/frontend/e2e
```

`D5` active worker entrypoint/import inventory:

```bash
sed -n '35,55p' pyproject.toml
rg -n "from workers\\.|workers\\.(forcing_producer|shud_runtime|output_parser|flood_frequency)|forcing_producer|shud_runtime|output_parser|flood_frequency" services workers tests apps scripts pyproject.toml --glob '!**/__pycache__/**'
```

`D6` Slurm template counterpart inventory:

```bash
find workers/sbatch_templates infra/sbatch -maxdepth 1 -type f | sort
rg -n "template_dir|SLURM_GATEWAY_TEMPLATE_DIR|infra/sbatch|workers/sbatch_templates" services/slurm_gateway services/orchestrator tests infra config docs/modules docs/governance --glob '!**/__pycache__/**'
```

`D7` tile publisher counterpart inventory:

```bash
rg -n "tile|Tile|flood-return-period|FloodReturnPeriod|api/v1/tiles|services\.tiles|tile_publisher|publish_tiles|services/tile-publisher|services/tile_publisher" apps/api/routes/flood_alerts.py services/tiles apps/frontend/src/components/flood apps/frontend/src/components/map services/tile_publisher services/tile-publisher infra/sbatch/publish_tiles.sbatch services/orchestrator tests docs openspec --glob '!**/__pycache__/**' --glob '!apps/frontend/node_modules/**' --glob '!apps/frontend/dist/**'
```

`D8` QHH diagnostic inventory:

```bash
rg -n "DIAGNOSTIC-ONLY|run_qhh_continuous|run_qhh_cycle|run_qhh_backend_smoke|create_qhh_shud_manifest" scripts tests docs/runbooks docs/governance services/orchestrator --glob '!**/__pycache__/**'
```

`D9` paused CI inventory:

```bash
rg -n "frontend-m15-visual|&& false" .github/workflows/ci.yml CLAUDE.md progress.md openspec/changes/governance-2-legacy-dead-code-retirement --glob '!**/__pycache__/**'
```

`D10` QHH diagnostic helper dependency inventory:

```bash
rg -n "seed_qhh|reset_qhh|summarize_qhh|publish_qhh|apply_smoke_migrations|create_qhh_shud_manifest|run_qhh_backend_smoke|run_qhh_cycle" scripts docs/runbooks tests docs/governance --glob '!**/__pycache__/**'
```

## Required Discovery Results

`D1` produced references needed for downstream cleanup without another broad repository scan:

- Placeholder and legacy path references are concentrated in placeholder READMEs, `docs/modules/00_module_index.md`, historical OpenSpec fixtures, and this governance fixture.
- `services/slurm_gateway/config.py` records that `workers/sbatch_templates/` is legacy and that `SlurmGatewaySettings.template_dir` defaults to `infra/sbatch`.
- Active Slurm deployment and test evidence points to `infra/sbatch`:
  `infra/compose.compute.yml`, `infra/systemd/nhms-slurm-gateway.service`,
  `infra/docker/Dockerfile.app`, `tests/test_orchestrator.py`,
  `tests/test_analysis_pipeline.py`, `tests/test_real_slurm_gateway.py`, and
  `tests/test_slurm_route_contract.py`.
- QHH diagnostic references are headed by `DIAGNOSTIC-ONLY` markers in `scripts/run_qhh_continuous.py`, `scripts/run_qhh_cycle.sh`, and `scripts/create_qhh_shud_manifest.py`.
  `scripts/run_qhh_backend_smoke.sh` is documented by `docs/runbooks/qhh-backend-smoke.md` and `docs/runbooks/qhh-mvp-smoke-evidence.md` as live diagnostic/reproduction evidence, invokes `scripts/create_qhh_shud_manifest.py`, and is covered by static tests in `tests/test_qhh_scripts_static.py`.
  Static guard tests also exist in `tests/test_role_boundary_static.py`.
- Broad API route mocks exist in eight Playwright specs under `apps/frontend/e2e`.
  Paused CI evidence is captured by `D9`, because `D1` does not use `--hidden`
  and does not scan hidden `.github` paths.

`D2` confirmed both governed candidates and active counterparts are present, with generated directories intentionally excluded:

- Governed candidates: `apps/web`, `services/tile-publisher`, `workers/flood-frequency`, `workers/forcing-producer`, `workers/output-parser`, `workers/sbatch_templates`, and `workers/shud-runtime`.
- Active counterparts: `apps/frontend`, `services/tile_publisher`, `services/tiles`, `workers/flood_frequency`, `workers/forcing_producer`, `workers/output_parser`, and `workers/shud_runtime`.
- Test and workflow surfaces: `apps/frontend/e2e`, `scripts`, and `.github/workflows`.
- Excluded generated directories: `apps/frontend/node_modules`, `apps/frontend/dist`, and any `__pycache__` subtree.

`D3` confirmed the placeholder candidates are not full implementation trees:

- `apps/web` contains only `.gitkeep` and `README.md`.
- Each hyphenated worker placeholder contains `README.md` and `__init__.py`.
- `services/tile-publisher` contains `README.md` and `__init__.py`.

`D4` confirmed broad API mocks by exact spec:

- `apps/frontend/e2e/forecast.spec.ts`: 6 matches.
- `apps/frontend/e2e/flood-alerts.spec.ts`: 3 matches.
- `apps/frontend/e2e/hydro-met.spec.ts`: 1 match.
- `apps/frontend/e2e/m11-routes.spec.ts`: 4 matches.
- `apps/frontend/e2e/m15-visual-conformance.spec.ts`: 1 match.
- `apps/frontend/e2e/meteorology.spec.ts`: 1 match.
- `apps/frontend/e2e/monitoring.spec.ts`: 2 matches.
- `apps/frontend/e2e/preview-deeplink.spec.ts`: 2 matches.

`D6` confirmed `workers/sbatch_templates` contains legacy single-run templates, while `infra/sbatch` contains the canonical production template set including `publish_tiles.sbatch`, array templates, analysis templates, `hindcast.sbatch`, and `smoke.sbatch`.

`D7` confirmed both the tile publisher production counterpart and display tile consumers:

- `services/orchestrator/cli.py` and `services/orchestrator/chain.py` import `services.tile_publisher`; `infra/sbatch/publish_tiles.sbatch` invokes `nhms-pipeline publish-tiles`.
- `apps/api/routes/flood_alerts.py` imports `services.tiles.mvt` and owns `/api/v1/tiles/flood-return-period`, hydro, national hydro, and river-network tile routes.
- `services/tiles/mvt.py` provides MVT cache, metadata, SQL, and tile URL-template helpers for the display routes.
- `apps/frontend/src/components/flood` and `apps/frontend/src/components/map` consume flood-return-period tile metadata and render GeoJSON/MVT display layers.

`D9` confirmed `.github/workflows/ci.yml` contains the paused
`frontend-m15-visual` job with
`if: needs.changes.outputs.frontend == 'true' && false`.

`D10` confirmed the QHH diagnostic chain includes direct helper dependencies:

- `scripts/run_qhh_cycle.sh` directly invokes `scripts/apply_smoke_migrations.py`,
  `scripts/seed_qhh_forcing_stations.py`,
  `scripts/seed_qhh_shud_output_segments.py`,
  `scripts/create_qhh_shud_manifest.py`,
  `scripts/summarize_qhh_smoke_results.py`, and
  `scripts/publish_qhh_display_products.py`.
- `scripts/run_qhh_backend_smoke.sh` directly invokes the same helper chain and
  conditionally invokes `scripts/reset_qhh_smoke_db.py` when
  `QHH_RESET_SMOKE_DB=1`.
- `scripts/run_qhh_continuous.py` dispatches `scripts/run_qhh_cycle.sh` locally
  or through `scripts/run_qhh_cycle.sbatch`; the sbatch wrapper execs
  `scripts/run_qhh_cycle.sh` after sourcing its filtered QHH env file.
- `docs/runbooks/qhh-backend-smoke.md`, `docs/runbooks/qhh-continuous.md`,
  `docs/runbooks/qhh-mvp-smoke-evidence.md`, and
  `tests/test_qhh_scripts_static.py` record the evidence boundary for these
  scripts as diagnostic/reproduction, not production scheduler readiness.
- Related helper `scripts/seed_qhh_smoke_met_station.py` is not a direct
  dependency of the governed QHH entrypoints in current grep. It remains an
  out-of-chain QHH smoke helper unless #364 separately chooses to manifest,
  wrap, or retire it after a focused ownership check.

## Governed Inventory

| path | status | owner_area | active_counterpart | active build/import/deploy evidence | docs/runbook migration | proposed final action | verification_command |
|---|---|---|---|---|---|---|---|
| `apps/web` | `archived` | `display_readonly` | `apps/frontend` | `apps/web/README.md` states `apps/frontend/` is canonical and says this path is not used by active build, tests, or deployment. `D2` found `apps/frontend`; `apps/frontend/package.json` owns `build`, `test`, and e2e scripts. | Before #363 removes or archives it, update any stale docs that still point to `apps/web`; current module index already says `apps/frontend` is active. | #363 may delete or archive after stale docs are updated. This row is not approval to remove it. | `D1`, `D2`, `D3` |
| `workers/forcing-producer` | `archived` | `compute_control` | `workers/forcing_producer` | Placeholder README points to `workers/forcing_producer/`. `pyproject.toml` exposes `nhms-forcing = "workers.forcing_producer.cli:main"`. Scheduler, tests, and worker imports use the underscore package. | Keep docs and runbooks on `workers/forcing_producer`; remove historical hyphenated references before #363 cleanup. | #363 may delete or archive the placeholder after import and doc checks. | `D1`, `D2`, `D3`, `D5` |
| `workers/shud-runtime` | `archived` | `compute_control` | `workers/shud_runtime` | Placeholder README points to `workers/shud_runtime/`. `pyproject.toml` exposes `nhms-shud-runtime = "workers.shud_runtime.cli:main"`. Runtime tests and orchestrator paths import underscore package modules. | Keep docs and runbooks on `workers/shud_runtime`; remove historical hyphenated references before #363 cleanup. | #363 may delete or archive the placeholder after import and doc checks. | `D1`, `D2`, `D3`, `D5` |
| `workers/output-parser` | `archived` | `compute_control` | `workers/output_parser` | Placeholder README points to `workers/output_parser/`. `pyproject.toml` exposes `nhms-parse = "workers.output_parser.cli:main"`. Tests and worker chain imports use the underscore package. | Keep docs and runbooks on `workers/output_parser`; remove historical hyphenated references before #363 cleanup. | #363 may delete or archive the placeholder after import and doc checks. | `D1`, `D2`, `D3`, `D5` |
| `workers/flood-frequency` | `archived` | `compute_control` | `workers/flood_frequency` | Placeholder README points to `workers/flood_frequency/`. `pyproject.toml` exposes `nhms-flood = "workers.flood_frequency.cli:main"`. Flood, hindcast, return-period, API, and test imports use the underscore package. | Keep docs and runbooks on `workers/flood_frequency`; remove historical hyphenated references before #363 cleanup. | #363 may delete or archive the placeholder after import and doc checks. | `D1`, `D2`, `D3`, `D5` |
| `workers/sbatch_templates` | `archived` | `slurm_gateway` | `infra/sbatch`; `services/slurm_gateway/config.py`; `SLURM_GATEWAY_TEMPLATE_DIR` | Contains legacy single-run `.sbatch` templates, so it is not an empty placeholder. `services/slurm_gateway/config.py` says it is legacy and not used by M3+ array orchestration; `SlurmGatewaySettings.template_dir` defaults to `infra/sbatch`. Active deploy/config references point to `infra/sbatch` through compute env, compose, systemd, Dockerfile, and gateway tests. | Keep migration notes pointing to `infra/sbatch`, `DEFAULT_JOB_TYPE_TEMPLATES`, and `SLURM_GATEWAY_TEMPLATE_DIR`. Any archive/delete PR must preserve the legacy-template manifest first. | #363 may archive or delete only after a stronger migration note and no-active-dependency proof. It must not be treated like the empty placeholders. | `D1`, `D2`, `D6` |
| `services/tile-publisher` | `archived` | `compute_control` | `services/tile_publisher`; `infra/sbatch/publish_tiles.sbatch`; display API/frontend tile consumers | Placeholder README states there is no active Python package under the hyphenated path. Active imports use `services.tile_publisher` from orchestrator CLI/chain and tests; active Slurm uses `infra/sbatch/publish_tiles.sbatch`; display consumption uses API tile routes and frontend map/flood components. | Keep docs on `services/tile_publisher`, `publish_tiles.sbatch`, and display API/frontend paths. Update historical OpenSpec references before #363 cleanup. | #363 may archive or delete the hyphenated placeholder after docs are aligned and active import evidence is rechecked. | `D1`, `D2`, `D3`, `D7` |
| `scripts/run_qhh_continuous.py` | `diagnostic` | `compute_control` | Production scheduler/orchestrator paths under `services/orchestrator`; canonical Slurm templates under `infra/sbatch` | File has a `DIAGNOSTIC-ONLY` header. `docs/runbooks/qhh-continuous.md` and `docs/runbooks/qhh-22-business-bringup.md` describe it as QHH bring-up, diagnostic, reproduction, or evidence path. It dispatches `scripts/run_qhh_cycle.sh` locally or through `scripts/run_qhh_cycle.sbatch`, so its direct helper dependency chain is the cycle script chain recorded by `D10`. Static tests keep QHH diagnostic tokens out of production orchestrator code. | #364 should add or update a diagnostic manifest and point production runbooks to generic scheduler/orchestrator commands. | Keep available until #364 explicitly retires, wraps, or relocates it with compatibility notes. This row is not approval to move it. | `D1`, `D8`, `D10` |
| `scripts/run_qhh_cycle.sh` | `diagnostic` | `compute_control` | Production scheduler/orchestrator paths under `services/orchestrator`; canonical Slurm templates under `infra/sbatch` | File has a `DIAGNOSTIC-ONLY` header. It invokes `scripts/apply_smoke_migrations.py`, `scripts/seed_qhh_forcing_stations.py`, `scripts/seed_qhh_shud_output_segments.py`, `scripts/create_qhh_shud_manifest.py`, `scripts/summarize_qhh_smoke_results.py`, and `scripts/publish_qhh_display_products.py`; it is called by the diagnostic continuous runner and diagnostic sbatch wrapper. Static tests cover QHH diagnostic script tokens. | #364 should keep the runbook migration explicit: diagnostic reproduction remains separate from production automation, and helper dependencies must move only with wrappers or runbook migration. | Keep available until #364 explicitly retires, wraps, or relocates it with compatibility notes. | `D1`, `D8`, `D10` |
| `scripts/run_qhh_cycle.sbatch` | `diagnostic` | `compute_control` | Production scheduler/orchestrator paths under `services/orchestrator`; canonical Slurm templates under `infra/sbatch` | Companion diagnostic sbatch invokes `scripts/run_qhh_cycle.sh`, so its helper dependency chain is the cycle script chain recorded by `D10`. It is documented by QHH diagnostic runbooks and is not a canonical gateway-owned `infra/sbatch` production template. | #364 should include it in the diagnostic manifest if QHH script isolation changes path layout. | Keep available unless #364 replaces it with wrappers and runbook migration. | `D1`, `D8`, `D10` |
| `scripts/run_qhh_backend_smoke.sh` | `diagnostic` | `compute_control` | Production scheduler/orchestrator paths under `services/orchestrator`; canonical Slurm templates under `infra/sbatch` | Backend-smoke script invokes `scripts/apply_smoke_migrations.py`, `scripts/reset_qhh_smoke_db.py` when `QHH_RESET_SMOKE_DB=1`, `scripts/seed_qhh_forcing_stations.py`, `scripts/seed_qhh_shud_output_segments.py`, `scripts/create_qhh_shud_manifest.py`, `scripts/summarize_qhh_smoke_results.py`, and `scripts/publish_qhh_display_products.py` as part of a live diagnostic/reproduction chain. `docs/runbooks/qhh-backend-smoke.md` documents it as QHH GFS backend-smoke live diagnostic/reproduction evidence, and `docs/runbooks/qhh-mvp-smoke-evidence.md` records the `Q214-GFS-01` diagnostic evidence boundary. `tests/test_qhh_scripts_static.py` covers backend-smoke script assumptions while static guard tests keep diagnostic manifest-builder tokens out of production orchestrator code. | #364 should include backend-smoke and its direct helper chain in any QHH diagnostic manifest and keep production scheduler/runbook replacement separate from this reproduction evidence. | Keep available until #364 explicitly retires, wraps, or relocates it with compatibility notes and production replacement evidence. | `D1`, `D8`, `D10` |
| `scripts/create_qhh_shud_manifest.py` | `diagnostic` | `compute_control` | Production manifest generation under orchestrator/model-registry paths, not this standalone QHH helper | File has a `DIAGNOSTIC-ONLY` header. `scripts/run_qhh_cycle.sh` and `scripts/run_qhh_backend_smoke.sh` call it; static tests assert production orchestrator sources do not invoke the diagnostic manifest builder. | #364 should state the production manifest replacement and keep the diagnostic manifest-builder status explicit. | Keep available until #364 explicitly retires, wraps, or relocates it with production replacement evidence. | `D1`, `D8`, `D10` |
| `scripts/apply_smoke_migrations.py` | `diagnostic` | `compute_control` | Production migrations through `packages.common.migrate`; target production PostgreSQL/TimescaleDB migration process | Local-only QHH smoke compatibility runner for databases with PostGIS but without TimescaleDB. `scripts/run_qhh_cycle.sh` and `scripts/run_qhh_backend_smoke.sh` invoke it when `QHH_USE_SMOKE_MIGRATIONS=1`; `docs/runbooks/qhh-backend-smoke.md` records it as a smoke-environment compatibility fix. | #364 should list it as a direct helper dependency of QHH diagnostic scripts if paths move, while production migration docs remain on `packages.common.migrate` and target-env DB migration evidence. | Keep available until #364 explicitly retires, wraps, or relocates the QHH diagnostic chain with migration notes. | `D10` |
| `scripts/reset_qhh_smoke_db.py` | `diagnostic` | `compute_control` | Production state cleanup and lifecycle handling through orchestrator/database ownership, not this qhh smoke reset helper | `scripts/run_qhh_backend_smoke.sh` invokes it only when `QHH_RESET_SMOKE_DB=1` for repeatable QHH smoke reruns. `docs/runbooks/qhh-backend-smoke.md` records that it deletes only qhh smoke-related registry, forcing, run, timeseries, and QC rows. | #364 should keep repeatable backend-smoke reset ownership explicit if backend-smoke is moved, wrapped, or retired. It must not be documented as a production reset command. | Keep available unless #364 replaces backend-smoke repeatability with an equivalent diagnostic reset path and runbook migration. | `D10` |
| `scripts/seed_qhh_forcing_stations.py` | `diagnostic` | `compute_control` | `workers.model_registry.qhh_production_bootstrap.seed_qhh_forcing_stations`; production registry/bootstrap ownership for QHH model metadata | Direct helper invoked by `scripts/run_qhh_cycle.sh` and `scripts/run_qhh_backend_smoke.sh` after registry import. It seeds QHH forcing stations from `qhh.tsd.forc`; `docs/runbooks/qhh-backend-smoke.md` records the 386-station diagnostic evidence boundary and standard SHUD forcing layout. | #364 should preserve this helper dependency or replace it with a documented production/bootstrap counterpart before moving or retiring QHH diagnostics. | Keep available until #364 explicitly retires, wraps, or relocates the QHH diagnostic chain with station-seeding evidence. | `D10` |
| `scripts/seed_qhh_shud_output_segments.py` | `diagnostic` | `compute_control` | `workers.model_registry.qhh_production_bootstrap.seed_qhh_output_segments`; production registry/bootstrap ownership for QHH output river identities | Direct helper invoked by `scripts/run_qhh_cycle.sh` and `scripts/run_qhh_backend_smoke.sh` after QHH package publication. `docs/runbooks/qhh-backend-smoke.md` records it as the helper that aligns SHUD `.sp.riv` output identities with parser output, and `tests/test_qhh_scripts_static.py` covers the output-row offset invariant. | #364 should preserve this helper dependency or replace it with a documented production/bootstrap counterpart before moving or retiring QHH diagnostics. | Keep available until #364 explicitly retires, wraps, or relocates the QHH diagnostic chain with output-segment evidence. | `D10` |
| `scripts/summarize_qhh_smoke_results.py` | `diagnostic` | `compute_control` | Production read APIs and pipeline evidence readers, not this standalone qhh smoke summary helper | Direct helper invoked by `scripts/run_qhh_cycle.sh` and `scripts/run_qhh_backend_smoke.sh` after SHUD output parsing. It reads QHH run, river timeseries, and QC rows and writes `qhh-result-summary.json` under the QHH run root. | #364 should keep summary artifact ownership explicit if QHH diagnostic evidence paths move or are wrapped. | Keep available until #364 explicitly retires, wraps, or relocates the QHH diagnostic chain with evidence artifact migration. | `D10` |
| `scripts/publish_qhh_display_products.py` | `diagnostic` | `compute_control` | Production display publication through orchestrator, flood-frequency worker, API routes, and frontend display consumers | Direct helper invoked by `scripts/run_qhh_cycle.sh` and `scripts/run_qhh_backend_smoke.sh` after QHH result summary. `docs/runbooks/qhh-backend-smoke.md` records that it activates the QHH model for API/frontend discovery, normalizes the scenario, computes return-period display rows with `no_frequency_curve` quality state, and writes `qhh-display-products.json`. | #364 should include this display-product helper in any diagnostic manifest and keep production display-readiness proof separate from QHH smoke publication evidence. | Keep available until #364 explicitly retires, wraps, or relocates the QHH diagnostic chain with display evidence migration. | `D10` |
| `apps/frontend/e2e/forecast.spec.ts` | `test-only` | `display_readonly` | Future live display-readonly e2e profile; current app under `apps/frontend` | `D4` found 6 broad `page.route('**/api/v1/**')` mocks. It is useful deterministic frontend regression evidence, not live API evidence. | #365 should rename or group as mocked regression and keep live receipt docs separate. | #365 owns any rename/config split/no-mock guard. | `D1`, `D4` |
| `apps/frontend/e2e/flood-alerts.spec.ts` | `test-only` | `display_readonly` | Future live display-readonly e2e profile; current app under `apps/frontend` | `D4` found 3 broad API route mocks. It cannot be cited as live display proof. | #365 should rename or group as mocked regression and keep live flood display receipt docs separate. | #365 owns any rename/config split/no-mock guard. | `D1`, `D4` |
| `apps/frontend/e2e/hydro-met.spec.ts` | `test-only` | `display_readonly` | Future live display-readonly e2e profile; current app under `apps/frontend` | `D4` found 1 broad API route mock. `docs/bugs.md` already records that existing specs with this pattern are mocked regression, not live receipt. | #365 should rename or group as mocked regression and update validation docs accordingly. | #365 owns any rename/config split/no-mock guard. | `D1`, `D4` |
| `apps/frontend/e2e/m11-routes.spec.ts` | `test-only` | `display_readonly` | Future live display-readonly e2e profile; current app under `apps/frontend` | `D4` found 4 broad API route mocks, including an abort route. It is deterministic route regression evidence, not live API proof. | #365 should preserve regression value while separating live route evidence. | #365 owns any rename/config split/no-mock guard. | `D1`, `D4` |
| `apps/frontend/e2e/m15-visual-conformance.spec.ts` | `test-only` | `display_readonly` | Current frontend app under `apps/frontend`; future live visual/e2e profile if retained | `D4` found 1 broad API route mock. The CI job for this spec is paused separately in `.github/workflows/ci.yml`. | #365 should classify it as mocked visual regression unless a live profile replaces it. #366 owns the paused CI lane. | #365 owns spec classification; #366 owns CI behavior. | `D1`, `D4`, `D9` |
| `apps/frontend/e2e/meteorology.spec.ts` | `test-only` | `display_readonly` | Future live display-readonly e2e profile; current app under `apps/frontend` | `D4` found 1 broad API route mock. It is mocked frontend regression, not live API evidence. | #365 should rename or group as mocked regression. | #365 owns any rename/config split/no-mock guard. | `D1`, `D4` |
| `apps/frontend/e2e/monitoring.spec.ts` | `test-only` | `display_readonly` | Future live display-readonly e2e profile; current app under `apps/frontend` | `D4` found 2 broad API route mocks. `docs/bugs.md` already records that these mocks do not connect to local real API, shared PostgreSQL, or Slurm. | #365 should rename or group as mocked regression and update validation docs. | #365 owns any rename/config split/no-mock guard. | `D1`, `D4` |
| `apps/frontend/e2e/preview-deeplink.spec.ts` | `test-only` | `display_readonly` | `apps/frontend/playwright.preview.config.ts`; future live preview/e2e profile if needed | `D4` found 2 broad API route mocks. `apps/frontend/playwright.preview.config.ts` limits this preview profile to `preview-deeplink.spec.ts`, but the spec still mocks API responses. | #365 should classify preview mocked regression separately from any live display-readonly proof. | #365 owns any rename/config split/no-mock guard. | `D1`, `D4` |
| `.github/workflows/ci.yml` | `test-only` | `display_readonly` | Active `frontend-build` job; future manual visual workflow or archived evidence | Job `frontend-m15-visual` is paused with `if: needs.changes.outputs.frontend == 'true' && false`. Comments say it is M15-specific legacy visual evidence and not node-27 or m25 frontend production. | #366 should replace the hidden false condition with archived documentation or an explicit manual workflow. | #366 owns removal, archive, manual dispatch, or CI behavior change. This row is not approval to edit CI in #362. | `D9` |

## QHH Out-of-Chain Note

The related helper below is not a direct dependency of the governed QHH entrypoints
in current `D10` results. It is recorded so #364 can decide whether to manifest,
wrap, or retire it without another broad discovery pass.

| path | status | owner_area | chain classification | rationale | follow-up ownership | verification_command |
|---|---|---|---|---|---|---|
| `scripts/seed_qhh_smoke_met_station.py` | `diagnostic` | `compute_control` | Related QHH smoke helper, out of the governed direct dependency chain | `D10` found no invocation from `scripts/run_qhh_continuous.py`, `scripts/run_qhh_cycle.sh`, `scripts/run_qhh_cycle.sbatch`, `scripts/run_qhh_backend_smoke.sh`, or `scripts/create_qhh_shud_manifest.py`. Historical OpenSpec notes mention its `forcing_proxy` seed behavior, while the current governed chain uses `scripts/seed_qhh_forcing_stations.py` for standard QHH forcing stations. | #364 should decide whether to list it in a diagnostic manifest as out-of-chain, archive it with evidence, or leave it documented as a standalone QHH smoke helper. #362 makes no runtime/path change. | `D10` |

## Active Counterparts

These are not cleanup candidates in #362. They are recorded so follow-up issues can migrate references without rediscovering the active path.

| path | status | owner_area | active build/import/deploy evidence | docs/runbook migration | proposed final action | verification_command |
|---|---|---|---|---|---|---|
| `apps/frontend` | `production` | `display_readonly` | `apps/frontend/package.json` owns `build`, `test`, `test:e2e`, and preview scripts. CI `frontend-build` installs, builds, tests, and checks bundle size from this directory. | Replacement for `apps/web`. | Retain active path; no #362 cleanup action. | `D1`, `D2` |
| `workers/forcing_producer` | `production` | `compute_control` | `pyproject.toml` exposes `nhms-forcing`; scheduler and tests import underscore package modules. | Replacement for `workers/forcing-producer`. | Retain active path; no #362 cleanup action. | `D2`, `D5` |
| `workers/shud_runtime` | `production` | `compute_control` | `pyproject.toml` exposes `nhms-shud-runtime`; runtime and tests import underscore package modules. | Replacement for `workers/shud-runtime`. | Retain active path; no #362 cleanup action. | `D2`, `D5` |
| `workers/output_parser` | `production` | `compute_control` | `pyproject.toml` exposes `nhms-parse`; parser tests and worker-chain smoke paths import underscore package modules. | Replacement for `workers/output-parser`. | Retain active path; no #362 cleanup action. | `D2`, `D5` |
| `workers/flood_frequency` | `production` | `compute_control` | `pyproject.toml` exposes `nhms-flood`; flood, hindcast, return-period, API, and tests import underscore package modules. | Replacement for `workers/flood-frequency`. | Retain active path; no #362 cleanup action. | `D2`, `D5` |
| `infra/sbatch` | `production` | `slurm_gateway` | `services/slurm_gateway/config.py` defaults `template_dir` to `infra/sbatch`; compute env, compose, systemd, Docker image copy, and tests reference this directory. | Replacement for `workers/sbatch_templates`. | Retain active path and canonical template ownership; no #362 cleanup action. | `D1`, `D6` |
| `services/slurm_gateway/config.py` | `production` | `slurm_gateway` | Owns `DEFAULT_JOB_TYPE_TEMPLATES` and `SlurmGatewaySettings.template_dir`; env prefix maps `SLURM_GATEWAY_TEMPLATE_DIR` to the same setting. | Source of truth for template migration and gateway defaults. | Retain active path and defaults; no #362 cleanup action. | `D1`, `D6` |
| `services/tile_publisher` | `production` | `compute_control` | Orchestrator CLI/chain import `services.tile_publisher`; tests cover tile publication behavior. | Replacement for `services/tile-publisher`. | Retain active path; no #362 cleanup action. | `D2`, `D7` |
| `infra/sbatch/publish_tiles.sbatch` | `production` | `slurm_gateway` | `services/slurm_gateway/config.py` maps `publish_tiles` to this template; `tests/test_slurm_array_contract.py` covers the publish command path. | Active Slurm entry for tile publication. | Retain active path; no #362 cleanup action. | `D1`, `D6`, `D7` |
| `apps/api/routes/flood_alerts.py` | `production` | `display_readonly` | Implements display flood alert and tile API routes used by frontend flood/map surfaces. | Active display API counterpart for legacy tile-publisher placeholder claims. | Retain active path; no #362 cleanup action. | `D7` |
| `services/tiles` | `production` | `display_readonly` | Provides tile helpers used by `apps/api/routes/flood_alerts.py`. | Active display tile implementation counterpart. | Retain active path; no #362 cleanup action. | `D2`, `D7` |
| `apps/frontend/src/components/flood` | `production` | `display_readonly` | Frontend flood alert components consume display API and tile products. | Active display frontend counterpart. | Retain active path; no #362 cleanup action. | `D7` |
| `apps/frontend/src/components/map` | `production` | `display_readonly` | Map components render active display layers and MVT/GeoJSON overlays. | Active display frontend counterpart. | Retain active path; no #362 cleanup action. | `D7` |

## Follow-Up Ownership

- #363 may act on `apps/web`, hyphenated worker placeholders, `workers/sbatch_templates`, and `services/tile-publisher`, but must re-run focused import/doc checks and update docs in the same PR.
- #364 may act on QHH diagnostics, but must preserve diagnostic value or add wrappers and runbook migration if paths move.
- #365 may split Playwright mocked regression from live display-readonly evidence, but must not cite specs with broad `page.route('**/api/v1/**')` as live receipt.
- #366 may remove `&& false`, archive visual evidence, or create a manual workflow for `frontend-m15-visual`; #362 makes no CI behavior change.
