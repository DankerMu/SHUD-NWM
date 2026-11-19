# Validation Matrix

This repository keeps fast checks, generated contract checks, and opt-in real-asset smoke checks separate.

## Basins Asset Commands

Synthetic Basins tests use temporary fixtures and run in fast CI. Real `data/Basins` checks are opt-in because the repository does not vendor the production asset tree.

```bash
uv run nhms-model discover-basins --basins-root data/Basins --output /tmp/basins-inventory.json

OBJECT_STORE_ROOT=/tmp/nhms-object-store OBJECT_STORE_PREFIX=s3://nhms \
  uv run nhms-model publish-basins \
  --inventory /tmp/basins-inventory.json \
  --model-id basins_qhh_shud \
  --version vbasins-smoke \
  --output /tmp/basins-package-manifest.json

uv run nhms-model import-basins-registry \
  --inventory /tmp/basins-inventory.json \
  --package-manifest /tmp/basins-package-manifest.json \
  --database-url postgresql://nhms:nhms_dev@localhost:5432/nhms_scratch \
  --output /tmp/basins-registry-import-report.json \
  --auth-actor-id cli-model-admin \
  --auth-role model_admin

uv run nhms-model basins-migration-report \
  --basins-root /volume/data/nwm/Basins \
  --output /tmp/basins-migration-report.json
```

`import-basins-registry` mutates core registry tables and requires explicit CLI auth evidence. The
`--auth-actor-id` / `--auth-role` flags, or `NHMS_CLI_AUTH_ACTOR_ID` / `NHMS_CLI_AUTH_ROLES`, are
deterministic dev/test policy evidence only; production live authorization remains through protected API/live
IdP proof. Do not run it against production unless it is an intentional migration with backup, approval, and an
explicit production database URL.

Production migration evidence must point at a copied Basins directory. A symlink-only `/volume/data/nwm/Basins` target is rejected because Linux production hosts must copy the actual data, not only migrate the development symlink.

Known source quirks covered by discovery, packaging, import, and docs:

- Legacy `tailanhe/focing` is accepted as a forcing alias and recorded as a quirk.
- NAS/macOS sidecars `.DS_Store`, `@eaDir`, and `*@SynoEAStream` are ignored during discovery and checksum/count evidence.
- SHUD input aliases under `input/<alias>` are preserved through inventory, package manifests, registry import, API responses, and frontend generated types.
- Runtime package publication rejects unsafe symlink descendants; production copies must not rely on `/volume/data/nwm/Basins` symlinks.

## Backend Fast

No Docker, PostgreSQL, MinIO, Slurm, or external network is required.

```bash
uv run ruff check .
uv run pytest -q
```

Focused production display contract checks:

```bash
uv run pytest -q tests/test_api.py tests/test_openapi_drift.py tests/test_migrations.py
cd apps/frontend && corepack pnpm check:api-types
cd apps/frontend && corepack pnpm test
cd apps/frontend && corepack pnpm build
```

Production display readiness is proven by live target-environment API and
browser receipts, not by deterministic checks alone. National rendering should
use layer metadata from `/api/v1/layers` and MapLibre vector sources. Live
PostGIS, national-data, and browser proof remains opt-in and must be recorded
as `not_executed` or a release blocker until target-environment validation
passes; deterministic MVT evidence alone must not set
`production_mvt_readiness_claimed=true`.

Focused M9 Basins closeout checks:

```bash
# #1912/#1903: the publication corpus is seven partitioned suites; the core path alone is
# no longer the corpus and must never be used as shorthand for it.
# #1913: the registry-import corpus is seven partitioned suites; its retained
# historical path alone is likewise no longer the corpus.
uv run pytest -q \
  tests/test_basins_discovery.py \
  tests/test_basins_migration_report.py \
  tests/test_basins_package_forcing_identity.py \
  tests/test_basins_package_publication.py \
  tests/test_basins_package_publication_failures.py \
  tests/test_basins_package_publication_refusal.py \
  tests/test_basins_package_publication_rivseg.py \
  tests/test_basins_package_publication_toctou.py \
  tests/test_basins_registry_import.py \
  tests/test_basins_registry_import_auth.py \
  tests/test_basins_registry_import_cli.py \
  tests/test_basins_registry_import_db.py \
  tests/test_basins_registry_import_parser.py \
  tests/test_basins_registry_import_qhh.py \
  tests/test_basins_registry_import_security.py \
  tests/test_shud_runtime.py \
  tests/test_model_registration.py \
  tests/test_api_contract.py \
  tests/test_api_contract_pipeline_ops.py \
  tests/test_api_contract_resources.py \
  tests/test_openapi_drift.py
```

Focused M18 model asset lifecycle checks:

```bash
openspec validate --all --strict --no-interactive  # m18-model-asset-operations is archived; its specs are promoted
uv run pytest -q tests/test_model_registration.py tests/test_model_activation_audit_integration.py
uv run pytest -q tests/test_api_contract*.py tests/test_auth_policy_matrix.py
cd apps/frontend && corepack pnpm test
cd apps/frontend && corepack pnpm build
```

M18 mutates registry lifecycle state only. Supported operations are activate,
deactivate, switch version, rollback version, supersede, and deprecate, guarded
by M17 action ids and preflight/audit evidence. It does not upload arbitrary
model packages or delete/upload production object-store assets.

## Real Slurm Smoke

Use the real cluster smoke only on a host with Slurm CLI access. Keep log paths
on shared storage such as `/scratch/frd_muziyao/slurm-smoke/`; `/tmp` can be
compute-node-local and may not be readable from the login node after completion.

Observed test environment on 2026-05-16:

- Host/user: `xnode` / `frd_muziyao`.
- Cluster/account: `shudhpc`, default Slurm account `friends`.
- CLI tools: `/usr/bin/sinfo`, `/usr/bin/squeue`, `/usr/bin/sbatch`,
  `/usr/bin/sacct`, `/usr/bin/scancel`.
- Partitions: `CPU*` and `GPU`, both up with `10-00:00:00` time limit.
- Smoke job `5684` ran on `cn04` and completed with `COMPLETED` / `0:0`.

Non-destructive inspection commands:

```bash
sinfo -o '%P|%a|%l|%D|%t|%N'
squeue -u "$USER" -o '%i|%P|%j|%u|%T|%M|%D|%R'
sacctmgr show user "$USER" format=User,DefaultAccount,Admin,Cluster%20 -P
scontrol show config | rg 'ClusterName|SlurmctldHost|AccountingStorageType|JobAcctGatherType|SelectType'
```

Minimal shared-output smoke script:

```bash
mkdir -p /scratch/frd_muziyao/slurm-smoke
cat >/scratch/frd_muziyao/slurm-smoke/smoke.sbatch <<'EOF'
#!/usr/bin/env bash
#SBATCH --job-name=nhms-smoke
#SBATCH --partition=CPU
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=1
#SBATCH --time=00:02:00
#SBATCH --output=/scratch/frd_muziyao/slurm-smoke/slurm-%j.out
#SBATCH --error=/scratch/frd_muziyao/slurm-smoke/slurm-%j.err
set -euo pipefail
echo "SLURM_SMOKE_START $(date -Iseconds) host=$(hostname) job=${SLURM_JOB_ID:-none} cwd=$(pwd)"
python3 - <<'PY'
import os
import sys
print("PYTHON_OK", sys.version.split()[0], os.environ.get("SLURM_JOB_ID"))
PY
echo "SLURM_SMOKE_DONE $(date -Iseconds)"
EOF

jobid=$(sbatch --parsable /scratch/frd_muziyao/slurm-smoke/smoke.sbatch)
echo "$jobid"
sacct -j "$jobid" --format=JobIDRaw,JobName,Partition,State,ExitCode,Elapsed,NodeList -P
```

Expected result after completion: `State=COMPLETED`, `ExitCode=0:0`, stdout
contains `SLURM_SMOKE_START`, `PYTHON_OK`, and `SLURM_SMOKE_DONE`, and stderr is
empty. This only proves Slurm submission/accounting/log retrieval works; SHUD
solver runtime, job arrays, retry behavior, and production-scale logs still need
separate validation.

## M20 Production Scheduler Automation

Issues #192-#196 move the qhh GFS/IFS proof from basin-specific scripts into
the backend production scheduler. The scheduler evidence is operator-facing
review evidence for discovery, dry-run planning, Slurm preflight, submitted or
blocked candidates, task/accounting summaries, and readiness interpretation. It
must not by itself set `final_production_readiness_claimed=true`.

Fast scheduler dry-run validation is non-mutating. It reads registry and
pipeline state through `DATABASE_URL`, discovers GFS/IFS cycle candidates, writes
one scheduler evidence artifact, and exits without runtime side effects:

```bash
export DATABASE_URL=postgresql://nhms:nhms_dev@localhost:5432/nhms
export WORKSPACE_ROOT="${WORKSPACE_ROOT:-$PWD}"
uv run nhms-pipeline plan-production \
  --dry-run \
  --source gfs \
  --source IFS \
  --lookback-hours 24 \
  --cycle-lag-hours 6 \
  --max-cycles-per-source 1 \
  --workspace-root "$WORKSPACE_ROOT/.nhms-workspace"
```

The JSON response includes `status`, `pass_id`, `artifact_path`, `counts`,
`operator_filters`, `source_cycles`, `candidates`, `blocked_candidates`,
`skipped_candidates`, `duplicate_exclusions`, `execution_boundary`, and
`no_mutation_proof`. In dry-run mode the expected proof is:

```json
{
  "adapter_download_called": false,
  "slurm_submit_called": false,
  "slurm_status_sync_called": false,
  "slurm_cancellation_called": false,
  "shud_runtime_called": false,
  "hydro_result_table_writes": false,
  "met_result_table_writes": false,
  "pipeline_status_writes": false,
  "pipeline_event_writes": false
}
```

That means no download, no Slurm submit/status sync/cancellation, no SHUD run,
no hydro/met result mutation, and no pipeline status/event writes. Dry-run
output can still include blocked or skipped candidates, for example unavailable
IFS cycles, duplicate active model identities, active Slurm jobs, terminal
completed runs, explicit operator filters, and source/model
exclusions. These are scheduler evidence states, not fabricated `met.*` enum
values.

Use `--plan` only as a planning-only alias for `--dry-run`; it is reserved for
dry-run/no-mutation smoke or business validation evidence and must not be used
for real production submission.

Evidence layout:

- Lock: `<workspace_root>/scheduler/production-scheduler.lock`.
- Pass artifacts: `<workspace_root>/scheduler/evidence/<pass_id>.json`.
- Candidate identity:
  `{source_id}:{cycle_time_utc}:{model_id}:{scenario_id}`.
- Deterministic run and forcing IDs:
  `fcst_{source_lower}_{YYYYMMDDHH}_{model_id}` and
  `forc_{source_lower}_{YYYYMMDDHH}_{model_id}`.
- Runtime/model-run evidence: `model_run_evidence[]` records submitted,
  partial, blocked, failed, skipped, restart, Slurm job/task, log URI,
  accounting/resource, station-count, parser/frequency/display quality, and
  residual blocker details when available.
- Slurm preflight evidence: `slurm_preflight` records compute-node reachable
  `DATABASE_URL`, workspace/object-store/log/runtime roots, allowlisted sbatch
  templates, bounded safe env/export values, and blockers. Secret-shaped fields
  are redacted.
- Readiness marker: scheduler artifacts include deterministic readiness context
  and `production_ready=false`; accepted live receipts remain the only final
  production readiness proof.

Production submission uses the same backend scheduler entrypoint with dry-run
disabled. The current CLI flag for that is `--submit`, so run it only after the
Slurm/database/storage preflight values point at the target environment:

```bash
export DATABASE_URL=postgresql://nhms:<strong-password>@pg.cluster.example:5432/nhms
export NHMS_PRODUCTION_SLURM_ENABLED=1
export WORKSPACE_ROOT=/scratch/frd_muziyao/nhms-production
export OBJECT_STORE_ROOT=/scratch/frd_muziyao/nhms-production/object-store
export SLURM_SHARED_LOG_ROOT=/scratch/frd_muziyao/nhms-production/slurm-logs
export NHMS_RUNTIME_ROOT=/scratch/frd_muziyao/nhms-production/runtime

uv run nhms-pipeline plan-production \
  --submit \
  --source gfs \
  --source IFS \
  --lookback-hours 24 \
  --cycle-lag-hours 6 \
  --max-cycles-per-source 1 \
  --workspace-root "$WORKSPACE_ROOT"
```

Slurm mode rejects missing or localhost-only `DATABASE_URL`, missing or
out-of-root storage roots, unsafe templates, unsafe env/export values, and
secret-shaped model/package/output evidence before submission. A preflight
blocker produces scheduler evidence with `submitted_count=0` and no active
Slurm job.

Focused fast commands for #196 documentation and evidence review:

```bash
uv run pytest -q tests/test_production_scheduler.py
uv run ruff check .
openspec validate --all --strict --no-interactive  # m20-production-multibasin-continuous-automation is archived; its specs are promoted
```

## Opt-In Real Basins Smoke

Run only when `data/Basins` exists and points at an accessible Basins tree.

```bash
# #1912: `test_real_basins_package_smoke_opt_in` moved to
# tests/test_basins_migration_report.py; list every partition so the opt-in real smoke
# still runs after the split.
NHMS_RUN_BASINS_SMOKE=1 uv run pytest -q \
  tests/test_basins_discovery.py \
  tests/test_basins_migration_report.py \
  tests/test_basins_package_forcing_identity.py \
  tests/test_basins_package_publication.py \
  tests/test_basins_package_publication_failures.py \
  tests/test_basins_package_publication_refusal.py \
  tests/test_basins_package_publication_rivseg.py \
  tests/test_basins_package_publication_toctou.py
```

Real registry import smoke also needs a PostgreSQL/PostGIS integration database and is skipped by default. The smoke is an `integration` test, so it also needs the same integration gate as Backend Integration below; a bare `DATABASE_URL` is ignored and the smoke skips with exit 0:

```bash
export NHMS_RUN_REAL_BASINS_IMPORT=1
export NHMS_RUN_INTEGRATION=1
export NHMS_INTEGRATION_DATABASE_URL=postgresql://nhms:nhms_dev@localhost:5432/nhms
# #1913: the opt-in real-Basins import smoke lives in the DB partition.
uv run pytest -q tests/test_basins_registry_import_db.py
```

## Backend Integration

Requires a reachable PostgreSQL database with PostGIS and TimescaleDB available. The pytest fixture creates and drops a temporary database from the configured URL, applies migrations from zero, and seeds deterministic issue-126 data.

```bash
docker compose -f infra/docker-compose.dev.yml up -d db
export NHMS_RUN_INTEGRATION=1
export NHMS_INTEGRATION_DATABASE_URL=postgresql://nhms:nhms_dev@localhost:5432/nhms
uv run pytest -q -m integration
```

Integration tests are skipped unless `NHMS_RUN_INTEGRATION=1` is set.
Use `NHMS_INTEGRATION_DATABASE_URL` for the service database.
Generic `DATABASE_URL` is ignored for destructive create/drop setup unless
`NHMS_ALLOW_DATABASE_URL_INTEGRATION=1` is also set for a guarded compatibility
run. Plain `uv run pytest -q`, even with `DATABASE_URL` in the shell, remains
self-contained.

## OpenAPI And Frontend Types

OpenAPI is authoritative for frontend API types. After API contract changes, regenerate or check type freshness from `apps/frontend/`.

```bash
cd apps/frontend
corepack pnpm generate:api
corepack pnpm check:api-types
```

## Frontend

Run from `apps/frontend/` with pnpm through Corepack.

```bash
cd apps/frontend
corepack prepare pnpm@10.11.0 --activate
corepack pnpm install --frozen-lockfile
corepack pnpm test
corepack pnpm build
```

Focused M9 frontend asset fixture checks:

```bash
cd apps/frontend
corepack pnpm check:api-types
corepack pnpm test -- src/api/__tests__/modelAssets.test.ts src/stores/__tests__/modelAssets
corepack pnpm build
```

## Frontend E2E

Frontend Playwright evidence has separate lanes. Keep receipts labelled with the
lane that produced them:

- `mocked-regression`: deterministic regression coverage for mocked UI behavior.
  Broad API mocks such as `page.route('**/api/v1/**')` are allowed. Receipts from
  this lane are mocked evidence only and must not be cited as live receipts or
  live `display_readonly` proof. This lane is an automatic CI gate: the
  `frontend-e2e` matrix job in `.github/workflows/ci.yml` runs
  `pnpm run test:e2e:mocked-regression --shard=<k>/<N>` on every PR whose diff
  matches the `frontend` path filter. Playwright splits the lane by test into
  disjoint shards that together run all of it, one worker each, shown as
  `Frontend E2E (mocked) (<shard>)`; a failing mocked spec fails its shard's
  check, and every shard must be green. The shard count is the matrix list in
  `ci.yml`. The `Frontend Build` check (job `frontend-build`) no longer runs
  the lane: it covers typecheck, build, vitest and the bundle check.
- `preview`: browser coverage for preview or ephemeral frontend builds where API
  responses may still be simulated. Broad API mocks such as
  `page.route('**/api/v1/**')` are allowed. Receipts from this lane are preview
  evidence only and must not be cited as live receipts or live
  `display_readonly` proof.
- `visual`: screenshot, layout, and visual-regression coverage where stable
  fixtures may isolate frontend rendering. Broad API mocks such as
  `page.route('**/api/v1/**')` are allowed. Receipts from this lane are visual
  evidence only and must not be cited as live receipts or live
  `display_readonly` proof.
- `live-c4-display`: live display_readonly browser proof for `/` plus strict
  GFS/IFS `/ops` against explicit runtime frontend and API bindings. API mocks
  are not allowed: the static guard rejects broad `page.route('**/api/v1/**')`
  mocks and any direct `page`/`context` `route` or `routeFromHAR` call in
  `e2e/live-c4-display.spec.ts`. This lane can produce the C4 live
  `display_readonly` receipt.
- `live-river-click`: live `/` river-click GFS+IFS P95 proof against explicit
  runtime frontend and API bindings. Broad API mocks such as
  `page.route('**/api/v1/**')` are not allowed in `e2e/live-display.spec.ts`.
  This lane can produce only the river-click live receipt, not a `/ops` or
  `/monitoring` receipt.
- The live lanes stay out of automatic CI; their oracle is node-27. The
  original two-URL `/monitoring` live lane was retired by #3202: `/monitoring`
  is not open to `display_readonly`, so that lane could never PASS on the
  public deployment.

```bash
cd apps/frontend
corepack pnpm test:e2e
corepack pnpm run test:e2e:mocked-regression
```

Live display_readonly `/` + `/ops` browser evidence must use the dedicated C4
profile. It requires the five env keys below and has no local dev-server or
`https://api.example.test` fallback. A passing C4 receipt requires the browser
page itself to fetch `/api/v1/runtime/config` from the configured API binding
and receive `service_role` exactly `display_readonly`, then load `/ops` for the
current GFS and IFS identities with their status, stages, jobs and job logs.

```bash
cd apps/frontend
PLAYWRIGHT_LIVE_BASE_URL=https://display.example.internal \
PLAYWRIGHT_LIVE_API_BASE_URL=https://api.example.internal \
PLAYWRIGHT_LIVE_C4_BASIN_ID=<current-basin-id> \
PLAYWRIGHT_LIVE_C4_SEGMENT_ID=<current-segment-id> \
PLAYWRIGHT_LIVE_C4_RECEIPT_PATH=/absolute/private/nhms-frontend-c4-live-evidence-<run>.json \
  corepack pnpm run test:e2e:live-c4-display
```

The kept live configs do not exit when a URL is missing or malformed: they
substitute a `http://127.0.0.1:0` placeholder base URL, and their globalSetup
owner classifies the config before any browser work. A missing
`PLAYWRIGHT_LIVE_BASE_URL` or `PLAYWRIGHT_LIVE_API_BASE_URL` with a safe receipt
path publishes one `REQUIRED_ENV_MISSING` `BLOCKED` receipt; a missing receipt
path is a `BLOCKED` diagnostic with no file; a malformed URL (including
username/password userinfo) or an invalid pin is a `CONFIG_INVALID` `FAIL`.
Do not supply credentials through URL userinfo. Record unavailable live runtime
as `BLOCKED`, not `PASS`. RBAC `权限不足`, page-visible runtime config
unavailability, visible role selector or retry/cancel controls, any browser
request to `/api/v1/slurm/*`, or a non-GET/HEAD request to the API origin also
cannot be recorded as a live `PASS`. Live specs must not register broad
`page.route('**/api/v1/**')` mocks; those mocks are allowed only in mocked
regression, preview, or visual evidence lanes. Do not use
`--project=chromium` for mocked evidence; use
`--project=mocked-regression-chromium`.

The `/` river-click GFS+IFS P95 metric is a dedicated lane. It requires the
five env keys and must not be inferred from `/monitoring` or `/ops`.

```bash
cd apps/frontend
PLAYWRIGHT_LIVE_BASE_URL=https://display.example.internal \
PLAYWRIGHT_LIVE_API_BASE_URL=https://api.example.internal \
PLAYWRIGHT_LIVE_RIVER_BASIN_ID=<current-basin-id> \
PLAYWRIGHT_LIVE_RIVER_SEGMENT_ID=<current-segment-id> \
PLAYWRIGHT_LIVE_RIVER_CLICK_RECEIPT_PATH=/absolute/private/nhms-frontend-river-click-live-evidence-<run>.json \
  corepack pnpm run test:e2e:live-river-click
```

## OpenSpec

The milestone changes `m9-basins-model-assets` and `m10-production-closure` are archived, so validating them by name no longer resolves (`Unknown item`); their specs are promoted and covered by:

```bash
openspec validate --all --strict --no-interactive
```

## M9 Closeout Evidence

Local #139 closeout verification on 2026-05-16:

- `openspec validate m9-basins-model-assets --strict --no-interactive` -> `Change 'm9-basins-model-assets' is valid`.
- `uv run ruff check .` -> `All checks passed!`.
- `uv run pytest -q tests/test_basins_discovery.py tests/test_basins_package_publication.py tests/test_basins_registry_import.py tests/test_shud_runtime.py tests/test_model_registration.py tests/test_api_contract.py tests/test_openapi_drift.py` -> `173 passed, 8 skipped, 5 warnings`.
- `NHMS_RUN_BASINS_SMOKE=1 uv run pytest -q tests/test_basins_discovery.py tests/test_basins_package_publication.py` -> `80 passed`.
- `cd apps/frontend && corepack pnpm check:api-types` -> generated `/tmp/nhms-api-types.ts` matched `src/api/types.ts`.
- `cd apps/frontend && corepack pnpm test -- src/api/__tests__/modelAssets.test.ts src/stores/__tests__/modelAssets.test.ts` -> `15 passed`, `53 passed`.
- `cd apps/frontend && corepack pnpm build` -> Vite production build succeeded.
