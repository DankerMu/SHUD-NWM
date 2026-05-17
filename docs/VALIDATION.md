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
rendered sbatch script and the `NON_FINITE_FLOW` worker/QC error signature from
the intended malformed-output path. The rendered script emits this validation
failure only when the selected task manifest declares
`expected_outcome=controlled_failure`; that branch invokes the repository
output-parser `.rivqdown` parser on a minimal NaN fixture so the signature comes
from the QC/parser path rather than a bare shell shortcut. Ordinary task `1`
workloads do not get the validation marker. Use `--force` only for an
intentional rerun of an existing `run_id`; the default protects audit evidence
from accidental overwrite.

In submit mode, the manifest index rendered into `NHMS_MANIFEST_INDEX` is copied
under the configured shared workspace at
`<workspace_root>/runs/<run_id>/input/manifest_index.json` so compute nodes can
read it. Fake and no-submit preflight runs keep generated manifest inputs inside
the evidence lane and are planned/preflight-only, not publishable acceptance
evidence. If submit preflight is blocked, runtime manifests and the manifest
index also stay inside the evidence lane and are not written to the shared
workspace. If `sbatch` rejects the submission, the validator removes the shared
runtime manifests/index written for that attempted submission before writing the
blocked evidence bundle.

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

## M10 #148 Production Object Store Closure

Issue #148 adds an opt-in production closure lane for Basins copied-data and
production-like object-store evidence. The default command uses a synthetic
copied Basins root and a filesystem-backed local object store; it does not
require real S3/MinIO, PostGIS, a live API, production credentials, or a SHUD
solver.

Fast deterministic evidence command:

```bash
uv run nhms-production validate-object-store \
  --evidence-root artifacts/production-closure \
  --run-id local-148
```

Production-like preflight can point at a copied Basins root and an object URI
prefix:

```bash
export NHMS_RUN_PRODUCTION_CLOSURE=1
export NHMS_PRODUCTION_OBJECT_STORE_TARGET=local-production-like
export NHMS_PRODUCTION_OBJECT_STORE_ROOT=/scratch/frd_muziyao/nhms-object-store
export NHMS_PRODUCTION_OBJECT_STORE_PREFIX=s3://nhms-prod/basins-migration
export NHMS_PRODUCTION_OBJECT_STORE_CREDENTIAL_SOURCE=env-or-workload-identity
export NHMS_PRODUCTION_OBJECT_STORE_CLEANUP_POLICY=quarantine
export NHMS_PRODUCTION_BASINS_ROOT=/scratch/frd_muziyao/copied-Basins
export NHMS_PRODUCTION_BASINS_MODEL_ID=basins_qhh_shud
export NHMS_PRODUCTION_BASINS_VERSION=v$(date -u +%Y%m%dT%H%M%SZ)
uv run nhms-production validate-object-store \
  --evidence-root artifacts/production-closure \
  --run-id "$(date -u +m10-148-%Y%m%dT%H%M%SZ)"
```

### Fast Regression Commands

Local #148 verification uses these fast regression commands:

```bash
openspec validate m10-production-closure --strict --no-interactive
.venv/bin/ruff check services/production_closure workers/model_registry tests/test_production_object_store_validation.py tests/test_production_slurm_validation.py tests/test_basins_package_publication.py docs/VALIDATION.md
.venv/bin/pytest -q tests/test_production_object_store_validation.py tests/test_basins_package_publication.py tests/test_basins_registry_import.py tests/test_shud_runtime.py tests/test_model_registration.py tests/test_api_contract.py tests/test_openapi_drift.py tests/test_production_slurm_validation.py
```

The opt-in deterministic production-closure smoke is:

```bash
NHMS_RUN_PRODUCTION_CLOSURE=1 .venv/bin/nhms-production validate-object-store \
  --evidence-root artifacts/production-closure \
  --run-id local-148-production-closure
```

With synthetic local inputs it should report `status=ready`. When configured
with missing real copied-root inputs, it should remain deterministic by writing a
stable blocker bundle instead of fabricating production success.

The Basins root must be copied data. A symlink-only root is blocked with
`BASINS_MIGRATION_SYMLINK_TARGET` before package/import writes occur.

The bundle is written under
`artifacts/production-closure/<run_id>/object-store/` and contains:

- `preflight.json`: redacted object-store target/root/prefix, endpoint,
  credential source, cleanup policy, copied Basins root, selected model/version,
  source URI, and evidence root.
- `migration_report.json` or `migration_blocker.json`: reused M9 migration
  evidence for copied roots, including file/byte counts and checksums, or a
  stable blocker for symlink roots.
- `package_manifest.json` and `package_manifest_evidence.json`: redacted Basins
  package manifest evidence published to the production-like object URI prefix.
- `stored_object_verification.json`: rereads the stored manifest/package objects
  and verifies sizes and SHA-256 checksums from stored bytes.
- `registry_api_runtime_consumption.json`: local registry import-source
  preparation, optional live registry DB import, deterministic API contract
  fixture, and runtime staging/cfg-generation evidence. Fast mode records live
  DB import and live API execution as `not_executed` with
  `api_contract_source=local_import_source`; this proves the object-URI
  consumption contract without claiming live registry/API success. Set
  `NHMS_PRODUCTION_OBJECT_STORE_RUN_REGISTRY_IMPORT=1` and provide
  `NHMS_PRODUCTION_OBJECT_STORE_REGISTRY_DATABASE_URL` or `DATABASE_URL` to
  require a live registry import report. When that opt-in is enabled, missing or
  failed DB import blocks the bundle instead of falling back to local-only
  success; a successful import uses `api_contract_source=live_registry_import`
  and records inserted/updated/idempotency fields. The runtime smoke writes
  validation-only forcing under `runs/<run_id>/input/scratch/runtime-staging/`
  and refuses to overwrite an existing scratch object.
- `runtime_staging_manifest.json`: full runtime manifest written during local
  staging, including object URI inputs/outputs used by the generated SHUD
  runtime configuration.
- `cleanup_rollback.json`: simulated failure after partial object write with
  written keys/rows, cleanup or quarantine status, and
  `implicit_model_activation=false`.
- `environment.json` and `summary.json`: redacted command/environment metadata
  and evidence file index.

Secret-shaped userinfo, query strings, fragments, and sensitive assignment
values are redacted from evidence.

## M10 Live Meteorology Ingestion + QC Closure

Issue #149 adds an opt-in production closure lane for live meteorology source
configuration, deterministic production-like cycle ingestion, canonical product
lineage, forcing generation, and forcing QC. The default command is
self-contained: it does not require external network access, live source
credentials, real S3/MinIO, copied `/volume` data, PostGIS, a live API, Slurm,
or a SHUD solver.

Fast deterministic evidence command:

```bash
uv run nhms-production validate-met \
  --evidence-root artifacts/production-closure \
  --run-id local-149
```

Production-like preflight can explicitly select the source subset, cycle window,
object prefix, model fixture, and CLDAS restricted reason:

```bash
export NHMS_RUN_PRODUCTION_CLOSURE=1
export NHMS_PRODUCTION_MET_SOURCES=GFS,IFS,ERA5
export NHMS_PRODUCTION_MET_ACCESS_MODE=public-or-deterministic-fixture
export NHMS_PRODUCTION_MET_CACHED_FALLBACK_POLICY=deterministic_fixture
export NHMS_PRODUCTION_MET_CYCLE_START=2026-05-07T00:00:00Z
export NHMS_PRODUCTION_MET_CYCLE_END=2026-05-07T03:00:00Z
export NHMS_PRODUCTION_MET_FORECAST_HOURS=0,3
export NHMS_PRODUCTION_MET_OBJECT_PREFIX=s3://nhms-prod/met
export NHMS_PRODUCTION_MET_MODEL_ID=basins_qhh_shud_fixture
export NHMS_PRODUCTION_MET_MODEL_VERSION=vproduction-met-local
export NHMS_PRODUCTION_MET_CLDAS_RESTRICTED_REASON="CLDAS credentials/licensing not available"
uv run nhms-production validate-met \
  --evidence-root artifacts/production-closure \
  --run-id "$(date -u +m10-149-%Y%m%dT%H%M%SZ)"
```

The lane records GFS, IFS, ERA5, and CLDAS source states. Fast mode executes
`deterministic_fixture` for enabled GFS only, records enabled IFS/ERA5 as
`skipped` in this GFS-only raw/canonical/forcing lane, and records CLDAS as
`restricted`. `source_config.json` also includes `configured_execution_mode`
so configured deterministic IFS/ERA5 capability is visible without claiming
that source work executed. If source-specific live gates such as
`NHMS_PRODUCTION_MET_ALLOW_LIVE_NETWORK=1` and
`NHMS_PRODUCTION_MET_LIVE_GFS=1` are set, the current #149 lane records
`not_executed` rather than claiming live success; live network execution is left
to a later production executor.

### Fast Regression Commands

Local #149 verification uses these fast regression commands:

```bash
openspec validate m10-production-closure --strict --no-interactive
uv run ruff check .
.venv/bin/ruff check services/production_closure tests/test_production_met_validation.py docs/VALIDATION.md progress.md
.venv/bin/pytest -q tests/test_production_met_validation.py tests/test_production_slurm_validation.py tests/test_canonical_converter.py tests/test_forcing_producer.py tests/test_source_identity.py tests/test_gfs_adapter.py tests/test_ifs_adapter.py tests/test_era5_adapter.py
```

The opt-in deterministic production-closure smoke is:

```bash
NHMS_RUN_PRODUCTION_CLOSURE=1 .venv/bin/nhms-production validate-met \
  --evidence-root artifacts/production-closure \
  --run-id local-149-production-met
```

With synthetic local inputs it should report `status=ready`. Validation-created
raw/canonical/forcing scratch objects are written under
`artifacts/production-closure/<run_id>/met/local-object-store/` and use object
URIs scoped to `<object-prefix>/runs/<run_id>/met/...`. Reusing a run ID refuses
to overwrite the existing bundle unless `--force` is supplied.

The bundle is written under
`artifacts/production-closure/<run_id>/met/` and contains:

- `preflight.json`: redacted enabled source subset, access mode, cached fallback
  policy, cycle window, object prefix, selected deterministic Basins-backed
  model/version, CLDAS restricted reason, evidence root, and bounded resource
  limits.
- `source_config.json`: GFS, IFS, ERA5, and CLDAS source status, configured
  execution mode, and actual lane execution mode from
  `deterministic_fixture`, `live_executed`, `skipped`, `restricted`, or
  `not_executed`; credentials are represented by source names only.
- `raw_cycle_manifest.json`: bounded deterministic source cycle evidence with
  source ID, cycle time, forecast hours, file counts, byte counts, checksums,
  retry counts, raw/object URIs, and skipped/restricted source evidence.
- `canonical_products.json`: canonical GFS product metadata with source cycle,
  variables, units, time axis, object URI, checksum, lineage, and explicit
  malformed/missing raw failure metadata.
- `forcing_manifest.json` and `forcing_qc.json`: forcing package URI, manifest,
  checksum, continuity check, required variables, units, missing-value check,
  range checks, and pass/fail status.
- `best_available_lineage.json`: selected source per valid time plus explicit
  skipped/restricted reasons; it does not fabricate success for non-executed
  GFS, IFS, ERA5, or CLDAS sources.
- `environment.json` and `summary.json`: redacted command/environment metadata
  and evidence file index.

## M10 #150 Staging End-to-End Forecast/Analysis Closure

Issue #150 adds an opt-in staging E2E closure lane that records the bounded
source -> canonical -> forcing -> Slurm SHUD -> parse -> flood frequency -> tile
publish -> API/frontend chain under one evidence bundle. The default command is
self-contained and deterministic: it does not require external network, real
object storage, copied `/volume` data, PostGIS, real Slurm, a live SHUD solver,
or a running frontend server. It also does not claim live DB/API/Slurm/frontend
success unless those checks are represented by supplied evidence.

Fast deterministic evidence command:

```bash
uv run nhms-production validate-e2e \
  --evidence-root artifacts/production-closure \
  --run-id local-150
```

Production-like preflight can explicitly select source cycle, model set, DB
target, object prefix, Slurm partition/account, frontend API base, and optional
accepted #147/#148/#149 evidence roots:

```bash
export NHMS_RUN_PRODUCTION_CLOSURE=1
export NHMS_PRODUCTION_E2E_SOURCE_CYCLE=2026-05-07T00:00:00Z
export NHMS_PRODUCTION_E2E_MODEL_SET=basins_qhh_shud_fixture
export NHMS_PRODUCTION_E2E_DB_TARGET=staging
export NHMS_PRODUCTION_E2E_OBJECT_PREFIX=s3://nhms-prod/staging-e2e
export NHMS_PRODUCTION_E2E_SLURM_PARTITION=CPU
export NHMS_PRODUCTION_E2E_SLURM_ACCOUNT=friends
export NHMS_PRODUCTION_E2E_FRONTEND_API_BASE=https://staging-api.example/api/v1
uv run nhms-production validate-e2e \
  --evidence-root artifacts/production-closure \
  --run-id "$(date -u +m10-150-%Y%m%dT%H%M%SZ)"
```

The bundle is written under
`artifacts/production-closure/<run_id>/e2e/` and contains:

- `preflight.json`: redacted source cycle, model set, DB target, object prefix,
  Slurm partition/account, frontend API base, dependency evidence roots, and
  self-contained execution policy.
- `dependency_status.json`: supplied or deterministic-equivalent #147/#148/#149
  evidence status as `consumed`, `skipped`, `missing`, or `blocked`; it never
  fabricates live Slurm/object-store/met success.
- `stage_manifest.json`: statuses, blockers, inputs, outputs, object URIs, DB
  IDs, Slurm job ID, and derived `model_id`, `basin_version_id`, `segment_id`,
  `source/cycle_time`, `job_id`, and `layer_id` for download, canonical,
  forcing, slurm, parse, frequency, tile, API, and frontend stages.
- `shud_output_qc.json`: deterministic SHUD `.rivqdown` QC with stable blockers
  for missing `.rivqdown`, malformed columns, NaN/Inf, missing required output,
  count mismatch, and time-axis mismatch. Failed QC blocks parse, frequency,
  tile, API, and frontend publication for that run while retaining raw/log paths.
- `api_contract_evidence.json`: existing-contract API evidence derived from the
  bundle identifiers. Fast mode records deterministic contract evidence and
  `live_api_executed=false`; it does not add run_id-specific API filters.
- `frontend_smoke_evidence.json`: deterministic evidence-backed smoke lineage
  for map, forecast, monitoring, and alerts. Fast mode records
  `live_frontend_executed=false`, `mock_api_routes_used=false`, and does not
  claim staging frontend readiness from mock-only data.
- `environment.json` and `summary.json`: redacted command/environment metadata,
  stage statuses, blockers, object URIs, logs, QC result, tile artifacts, and
  evidence file index.

Reusing a run ID refuses to overwrite the existing bundle unless `--force` is
supplied. Unsafe run IDs are rejected before writes. Secret-shaped object/API,
Slurm, frontend, DB, and environment values are redacted from stdout and
evidence.

### Fast Regression Commands

Local #150 verification uses these fast regression commands:

```bash
openspec validate m10-production-closure --strict --no-interactive
.venv/bin/ruff check services/production_closure tests/test_production_e2e_validation.py docs/VALIDATION.md progress.md
.venv/bin/pytest -q tests/test_production_e2e_validation.py tests/test_production_slurm_validation.py tests/test_production_object_store_validation.py tests/test_production_met_validation.py tests/test_output_parser.py tests/test_flood_frequency.py tests/test_api_contract.py
```

The opt-in deterministic production-closure smoke is:

```bash
NHMS_RUN_PRODUCTION_CLOSURE=1 .venv/bin/nhms-production validate-e2e \
  --evidence-root artifacts/production-closure \
  --run-id local-150-production-e2e
```

After using the smoke run locally, remove
`artifacts/production-closure/local-150-production-e2e/` so generated evidence
does not remain in the worktree.

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
