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
# #1912: the publication corpus is six partitioned suites; the core path alone is
# no longer the corpus and must never be used as shorthand for it.
uv run pytest -q \
  tests/test_basins_discovery.py \
  tests/test_basins_migration_report.py \
  tests/test_basins_package_forcing_identity.py \
  tests/test_basins_package_publication.py \
  tests/test_basins_package_publication_failures.py \
  tests/test_basins_package_publication_refusal.py \
  tests/test_basins_package_publication_toctou.py \
  tests/test_basins_registry_import.py \
  tests/test_shud_runtime.py \
  tests/test_model_registration.py \
  tests/test_api_contract.py \
  tests/test_openapi_drift.py
```

Focused M18 model asset lifecycle checks:

```bash
openspec validate m18-model-asset-operations --strict --no-interactive
uv run pytest -q tests/test_model_registration.py tests/test_model_activation_audit_integration.py
uv run pytest -q tests/test_production_ops_validation.py tests/test_production_object_store_validation.py
uv run pytest -q tests/test_api_contract.py tests/test_auth_policy_matrix.py
cd apps/frontend && corepack pnpm test
cd apps/frontend && corepack pnpm build
```

M18 mutates registry lifecycle state only. Supported operations are activate,
deactivate, switch version, rollback version, supersede, and deprecate, guarded
by M17 action ids and preflight/audit evidence. It does not upload arbitrary
model packages or delete/upload production object-store assets. Production ops
validation includes deterministic model lifecycle drills for bad activation,
rollback, blocked deactivation, and idempotent repeat without live credentials.

Focused M19 production-readiness proof checks:

```bash
openspec validate m19-production-readiness-proof --strict --no-interactive
uv run pytest -q tests/test_production_readiness_validation.py
uv run pytest -q tests/test_production_ops_validation.py tests/test_production_object_store_validation.py tests/test_production_slurm_validation.py tests/test_production_met_validation.py tests/test_production_e2e_validation.py tests/test_production_scale_validation.py
uv run ruff check .
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

The six M10 production-closure lanes below are current validation owned by the child
document [docs/validation/production-closure.md](validation/production-closure.md): their current procedure, commands and acceptance
evidence live there. Each heading stays in this matrix as a link stub so its original
anchor slug keeps resolving.

## M10 #147 Production Slurm Closure

Current procedure, commands and acceptance evidence for this lane: [docs/validation/production-closure.md#m10-147-production-slurm-closure](validation/production-closure.md#m10-147-production-slurm-closure).

## M10 #148 Production Object Store Closure

Current procedure, commands and acceptance evidence for this lane: [docs/validation/production-closure.md#m10-148-production-object-store-closure](validation/production-closure.md#m10-148-production-object-store-closure).

## M10 Live Meteorology Ingestion + QC Closure

Current procedure, commands and acceptance evidence for this lane: [docs/validation/production-closure.md#m10-live-meteorology-ingestion--qc-closure](validation/production-closure.md#m10-live-meteorology-ingestion--qc-closure).

## M10 #150 Staging End-to-End Forecast/Analysis Closure

Current procedure, commands and acceptance evidence for this lane: [docs/validation/production-closure.md#m10-150-staging-end-to-end-forecastanalysis-closure](validation/production-closure.md#m10-150-staging-end-to-end-forecastanalysis-closure).

## M10 National Scale / MVT Performance Closure

Current procedure, commands and acceptance evidence for this lane: [docs/validation/production-closure.md#m10-national-scale--mvt-performance-closure](validation/production-closure.md#m10-national-scale--mvt-performance-closure).

## M10 Production Ops / Security / Runbook Closure

Current procedure, commands and acceptance evidence for this lane: [docs/validation/production-closure.md#m10-production-ops--security--runbook-closure](validation/production-closure.md#m10-production-ops--security--runbook-closure).

## M19 Production Readiness Proof

Issue #181 adds a consolidated `nhms-production validate-readiness` lane. It is
a release-review report generator: default runs are deterministic and ingest
receipts only. The command does not execute a live IdP, alert sink, backend
mutation, rollback drill, Slurm workload, object-store operation,
weather/source download, or real-national-data scan.

```bash
NHMS_RUN_PRODUCTION_CLOSURE=1 uv run nhms-production validate-readiness \
  --evidence-root artifacts/production-closure \
  --run-id local-181-production-readiness
```

Optional deterministic producer summaries can be supplied with
`--slurm-evidence-root`, `--object-store-evidence-root`,
`--source-evidence-root`, `--e2e-evidence-root`, and `--mvt-evidence-root`.

Optional live proof receipts are supplied as JSON strings or files:
`--auth-proof` / `--auth-proof-file`, `--alert-proof` /
`--alert-proof-file`, `--rollback-proof` / `--rollback-proof-file`,
`--slurm-proof` / `--slurm-proof-file`, `--object-store-proof` /
`--object-store-proof-file`, `--source-proof` / `--source-proof-file`,
`--e2e-proof` / `--e2e-proof-file`, `--mvt-proof` /
`--mvt-proof-file`, and `--target-env-proof` /
`--target-env-proof-file`. Receipt payloads are normalized into bounded raw
validation data before path/secret redaction, then redacted before writing
evidence; malformed or oversized receipts become stable `release_blocked`
evidence and never print tracebacks or raw secrets.

Live proof receipt acceptance is intentionally stricter than a placeholder
`accepted=true` flag. Every accepted receipt must use schema
`nhms.production_readiness.live_proof.v1`, bind to the expected readiness
surface, current readiness `run_id`, target environment, and live proof
execution mode, and include semantic artifact/evidence references. Empty
containers, blank strings, `[null]`, and null-only mappings are not evidence.
Auth receipts must also include provider issuer/provider identity metadata,
role mapping with at least one role mapped to concrete actions/roles, and
allowed/denied coverage for every canonical protected action. Alert receipts
must include sink id/name/url/channel metadata plus delivery id, timestamp, and
result with delivered/passed status. Rollback receipts must include meaningful
preconditions, command or drill identity/command metadata, and an executed
result. Slurm/object-store/source/E2E/MVT dependency receipts must name the
expected dependency and bind to the producer contract: producer issue, producer
schema, producer run ID, producer artifact/path/ref, checksum or receipt ID,
target environment, and live proof mode. Top-level producer binding fields and
nested `provenance` binding fields are validated as one canonical receipt
contract: when both surfaces provide dependency, producer issue/schema/run ID,
artifact ref/path/URI, checksum, or receipt ID, they must agree after bounded
raw normalization and before public redaction, so distinct path-like aliases
cannot be collapsed into the same redacted token. Within either surface, every
supplied alias in a binding group is also validated; for example,
`producer_artifact_ref`, `summary_ref`,
`artifact_path`, and `artifact_uri` must all normalize to the same artifact
binding when more than one is present. The checksum/receipt-id alias group is
treated the same way. Sibling or contradictory nested provenance is a release
blocker even if a higher-priority top-level field is otherwise valid. When a
deterministic producer `summary.json` is supplied, every provided top-level and
nested provenance run ID, artifact ref, and checksum binding must also match
that consumed summary.
The deterministic producer `summary.json` alone does not satisfy live proof.
Target-environment receipts must include a concrete environment/config
identifier and meaningful target configuration metadata. Wrong schema, wrong
surface, stale run ID, deterministic mode, sibling dependency issue/schema/name,
contradictory provenance, missing target, missing
provenance/artifacts/checksum/ref/run ID, malformed JSON, over-size JSON, or
deeply nested JSON remains `release_blocked` with redacted bounded evidence.

Evidence is written under
`artifacts/production-closure/<run_id>/readiness/`:

- `preflight.json`: configured producer summary roots, receipt presence, and
  the no-live-side-effect fast-CI policy.
- `live_proof_receipts.json`: redacted, bounded receipt metadata and payloads.
- `readiness_items.json`: canonical readiness items with `surface`, `status`,
  `execution_mode`, `required_for_final`, `artifact_refs`, `residual_risk`,
  `removal_criteria`, `exclusions`, and `live_proof_accepted`.
- `release_blockers.json`: blocker id, surface, status, owner/action,
  residual risk, removal criteria, and artifact references.
- `environment.json`: redacted command environment and runtime metadata.
- `summary.json`: final interpretation, `final_production_readiness_claimed`,
  release blockers, and scoped exclusions.

Status values are `passed`, `failed`, `blocked`, `not_executed`, and
`release_blocked`. Execution modes are `deterministic`, `policy_simulated`,
`backend_route_executed`, `dry_run_sink`, `simulated_drill`, `live_proof`, and
`not_executed`. Deterministic items can pass and still leave
`final_production_readiness_claimed=false`; final readiness is true only when
every required live proof item is `passed` with `live_proof_accepted=true`.
Missing live IdP, alert sink, rollback, Slurm/object-store/source/E2E/MVT, or
target-environment config receipts are release blockers, not deterministic
failures.

CLDAS and incomplete real national data are explicit M19 scoped exclusions.
They are recorded as `not_executed` exclusions rather than failed deterministic
checks and do not satisfy live proof.

## M20 Production Scheduler Automation

Issues #192-#196 move the qhh GFS/IFS proof from basin-specific scripts into
the backend production scheduler. The scheduler evidence is operator-facing
review evidence for discovery, dry-run planning, Slurm preflight, submitted or
blocked candidates, task/accounting summaries, and readiness interpretation. It
does not replace M19 live proof receipts and must not by itself set
`final_production_readiness_claimed=true`.

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

Readiness validation remains M19-style. A fast readiness report can be generated
alongside scheduler evidence:

```bash
NHMS_RUN_PRODUCTION_CLOSURE=1 uv run nhms-production validate-readiness \
  --evidence-root artifacts/production-closure \
  --run-id local-m20-scheduler-readiness \
  --scheduler-evidence-root "$WORKSPACE_ROOT/.nhms-workspace/scheduler/evidence" \
  --force
```

This report writes `readiness/summary.json`,
`readiness/readiness_items.json`, `readiness/release_blockers.json`, and
`readiness/live_proof_receipts.json`. `--scheduler-evidence-root` ingests the
scheduler artifacts produced under the local fast workspace;
`--scheduler-evidence-file` can be used instead when review is pinned to one
artifact path. Omitting both scheduler evidence options intentionally produces an
M19-only readiness report.
Deterministic scheduler evidence is useful for release review and can be
referenced from live-proof receipt provenance, but fast evidence alone remains
non-final. The final readiness live-proof boundary is unchanged:
`final_production_readiness_claimed=true` requires accepted target environment
live receipts for the required M19 surfaces, with matching schema, run id, target
environment, producer artifact/ref/checksum, and live execution mode. Malformed,
oversized, stale, identity-mismatched, or deterministic-only scheduler evidence
is interpreted as blocked or release-blocked review evidence, not final
production readiness.

Focused fast commands for #196 documentation and evidence review:

```bash
uv run pytest -q tests/test_production_scheduler.py tests/test_production_readiness_validation.py
uv run ruff check .
openspec validate m20-production-multibasin-continuous-automation --strict --no-interactive
NHMS_RUN_PRODUCTION_CLOSURE=1 uv run nhms-production validate-readiness \
  --evidence-root artifacts/production-closure \
  --run-id local-m20-scheduler-readiness \
  --scheduler-evidence-root "$WORKSPACE_ROOT/.nhms-workspace/scheduler/evidence" \
  --force
```

## Legacy Production Ops Fast Regression Commands

Local #152 verification uses these fast regression commands:

```bash
openspec validate m10-production-closure --strict --no-interactive
uv run ruff check services/production_closure tests/test_production_ops_validation.py docs/VALIDATION.md docs/runbooks/api-latency.md docs/runbooks/tile-publish-error.md progress.md
uv run pytest -q tests/test_production_ops_validation.py
uv run pytest -q tests/test_production_ops_validation.py tests/test_production_scale_validation.py tests/test_production_e2e_validation.py tests/test_production_object_store_validation.py tests/test_production_met_validation.py tests/test_production_slurm_validation.py
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
  tests/test_basins_package_publication_toctou.py
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

Frontend Playwright evidence has separate lanes. Keep receipts labelled with the
lane that produced them:

- `mocked-regression`: deterministic regression coverage for mocked UI behavior.
  Broad API mocks such as `page.route('**/api/v1/**')` are allowed. Receipts from
  this lane are mocked evidence only and must not be cited as live receipts or
  live `display_readonly` proof.
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
- `live-display`: live display_readonly browser proof against explicit runtime
  frontend and API bindings. Broad API mocks such as
  `page.route('**/api/v1/**')` are not allowed and cannot produce live display
  receipts. Only this lane may produce live `display_readonly` receipts.

```bash
cd apps/frontend
corepack pnpm test:e2e
corepack pnpm run test:e2e:mocked-regression
```

Live display_readonly browser evidence must use the dedicated live profile. It
requires both runtime URLs and has no local dev-server or
`https://api.example.test` fallback. A passing live receipt requires the browser
page itself to fetch `/api/v1/runtime/config` from the configured API binding
and receive `service_role` exactly `display_readonly`, then fetch at least one
monitoring read API from that same binding. The runtime config body is the only
live response body parsed for evidence and must fit the bounded evidence size;
monitoring read API evidence records URL/status only. Both distinct API origins
and same-origin `/api` proxy deployments are valid when
`PLAYWRIGHT_LIVE_API_BASE_URL` names the expected API origin.

```bash
cd apps/frontend
PLAYWRIGHT_LIVE_BASE_URL=https://display.example.internal \
PLAYWRIGHT_LIVE_API_BASE_URL=https://api.example.internal \
  corepack pnpm run test:e2e:live-display
```

If either `PLAYWRIGHT_LIVE_BASE_URL` or `PLAYWRIGHT_LIVE_API_BASE_URL` is
missing, or either URL includes username/password userinfo,
`test:e2e:live-display` exits before browser execution with
`Live display Playwright profile BLOCKED` or a live display profile URL error.
Do not supply credentials through URL userinfo. Record unavailable live runtime
as `BLOCKED`, not `PASS`. RBAC `权限不足`, page-visible runtime config
unavailability, any browser request to `/api/v1/slurm/*`, or retry/cancel
mutations also cannot be recorded as a live `PASS`. Live-display specs must not
register broad `page.route('**/api/v1/**')` mocks; those mocks are allowed only
in mocked regression, preview, or visual evidence lanes. Do not use
`--project=chromium` for mocked evidence; use
`--project=mocked-regression-chromium`.

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
