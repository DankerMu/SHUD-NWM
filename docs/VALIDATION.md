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
  --output /tmp/basins-registry-import-report.json

uv run nhms-model basins-migration-report \
  --basins-root /volume/data/nwm/Basins \
  --output /tmp/basins-migration-report.json
```

`import-basins-registry` mutates core registry tables. Do not run it against production unless it is an intentional migration with backup, approval, and an explicit production database URL.

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

Focused M9 Basins closeout checks:

```bash
uv run pytest -q \
  tests/test_basins_discovery.py \
  tests/test_basins_package_publication.py \
  tests/test_basins_registry_import.py \
  tests/test_shud_runtime.py \
  tests/test_model_registration.py \
  tests/test_api_contract.py \
  tests/test_openapi_drift.py
```

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

## M10 #147 Production Slurm Closure

Issue #147 adds an opt-in production closure lane for the real Slurm + SHUD
workload evidence bundle. Default tests remain fake/deterministic and do not
require Slurm, a live SHUD solver, copied Basins root, object-store credentials,
external network, or production secrets.

Fast deterministic evidence command:

```bash
uv run nhms-production validate-slurm \
  --evidence-root artifacts/production-closure \
  --run-id local-147 \
  --fake-slurm
```

Production preflight command (does not submit work and is not #147 acceptance
evidence):

```bash
export NHMS_RUN_PRODUCTION_CLOSURE=1
export NHMS_PRODUCTION_SLURM_CLUSTER=shudhpc
export NHMS_PRODUCTION_SLURM_ACCOUNT=friends
export NHMS_PRODUCTION_SLURM_PARTITION=CPU
export NHMS_PRODUCTION_SLURM_WORKSPACE_ROOT=/scratch/frd_muziyao/nhms-production
export NHMS_PRODUCTION_SLURM_MODEL_ID=basins_qhh_shud
export NHMS_PRODUCTION_SLURM_MODEL_PACKAGE_URI=s3://nhms-prod/models/basins_qhh_shud/v1/package/
export NHMS_PRODUCTION_SLURM_WALLTIME=00:30:00
export NHMS_PRODUCTION_SLURM_CPUS_PER_TASK=2
export NHMS_PRODUCTION_SLURM_MEMORY_GB=8
export NHMS_PRODUCTION_SLURM_SHUD_THREADS=2
uv run nhms-production validate-slurm \
  --evidence-root artifacts/production-closure \
  --run-id "$(date -u +m10-147-%Y%m%dT%H%M%SZ)"
```

Production acceptance command:

```bash
uv run nhms-production validate-slurm \
  --evidence-root artifacts/production-closure \
  --run-id "$(date -u +m10-147-submit-%Y%m%dT%H%M%SZ)" \
  --submit \
  --poll-interval-seconds 15 \
  --poll-timeout-seconds 900
```

Poll options must be finite values. `--poll-interval-seconds` must be at least
`1` and at most `300` seconds; `--poll-timeout-seconds` is bounded from `0` to
`86400` seconds. Invalid values fail before writing evidence with
`PRODUCTION_SLURM_POLL_OPTION_INVALID`.

Submitted acceptance evidence requires terminal Slurm accounting rows for array
tasks `0` and `1`: task `0` must complete successfully, and task `1` must reach
the expected explicit worker failure (`FAILED` with a nonzero exit code).
Cancellation, timeout, node failure, preemption, or out-of-memory outcomes do
not satisfy the controlled-failure contract. Missing terminal task outcomes,
shared stdout/stderr logs, or QC blocking evidence block #147 acceptance. The
task `1` shared stdout/stderr evidence must include the
`NHMS_PRODUCTION_SLURM_CONTROLLED_FAILURE_EXPECTED` marker emitted by the
rendered sbatch script before the expected worker failure. Use `--force` only
for an intentional rerun of an existing `run_id`; the default protects audit
evidence from accidental overwrite.

In submit mode, the manifest index rendered into `NHMS_MANIFEST_INDEX` is copied
under the configured shared workspace at
`<workspace_root>/runs/<run_id>/input/manifest_index.json` so compute nodes can
read it. Fake and no-submit preflight runs keep generated manifest inputs inside
the evidence lane and are planned/preflight-only, not publishable acceptance
evidence. If submit preflight is blocked, runtime manifests and the manifest
index also stay inside the evidence lane and are not written to the shared
workspace.

If required preflight inputs or Slurm CLI tools are absent, the command writes a
clear blocker bundle under `artifacts/production-closure/<run_id>/slurm/` and
returns success so default validation does not fail unpredictably. The bundle
contains:

- `preflight.json`: redacted cluster/account/partition, shared workspace,
  solver, model package URI, walltime/resources, object roots, and evidence root.
- `rendered_run_shud_forecast_array.sbatch`: canonical `infra/sbatch` rendering
  with shared stdout/stderr, `cpus_per_task`, memory, walltime, `SHUD_THREADS`,
  `OMP_NUM_THREADS`, workspace/object roots, and manifest-index command.
- `manifest_index.json`: two-task array fixture for success and controlled
  failure; submit mode also copies this index into the shared workspace for the
  rendered sbatch script.
- `slurm_accounting.json`: fake or attached Slurm accounting fields with job ID,
  state, exit code, elapsed, node list, partition, and array task rows.
- `array_partial_success.json`: publishable sibling success and actionable
  failed task metadata. No-submit/preflight-only and blocked accounting bundles
  set `successful_outputs_remain_publishable=false` and do not invent concrete
  job IDs. Submitted evidence only marks task outputs publishable after the
  corresponding shared stdout/stderr logs are present and readable.
- `retry_cancel.json`: retry/cancel evidence that does not mutate successful
  outputs. Submitted runs mark retry/cancel as `not_executed` unless explicit
  real cancellation/retry evidence exists.
- `qc_blocking.json`: malformed SHUD output/QC blocking evidence for the
  affected task while sibling success remains publishable. Submitted runs mark
  this evidence verified only when the controlled-failure marker is present in
  task `1` shared logs.
- `environment.json` and `summary.json`: redacted command/environment metadata
  and evidence file index.

Secrets and signed URL-shaped values are redacted from the rendered script and
all JSON evidence touched by this lane.

## Opt-In Real Basins Smoke

Run only when `data/Basins` exists and points at an accessible Basins tree.

```bash
NHMS_RUN_BASINS_SMOKE=1 uv run pytest -q \
  tests/test_basins_discovery.py \
  tests/test_basins_package_publication.py
```

Real registry import smoke also needs a PostgreSQL/PostGIS integration database and is skipped by default:

```bash
export NHMS_RUN_REAL_BASINS_IMPORT=1
export DATABASE_URL=postgresql://nhms:nhms_dev@localhost:5432/nhms
uv run pytest -q tests/test_basins_registry_import.py
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
corepack pnpm test -- src/api/__tests__/modelAssets.test.ts src/stores/__tests__/modelAssets.test.ts
corepack pnpm build
```

## Frontend E2E

Use the existing Playwright scripts with the frontend preview server and any required API service for the target scenario.

```bash
cd apps/frontend
corepack pnpm test:e2e
```

## OpenSpec

```bash
openspec validate m9-basins-model-assets --strict --no-interactive
openspec validate m10-production-closure --strict --no-interactive
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
