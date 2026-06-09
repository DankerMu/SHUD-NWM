# Bugs Governance Ledger

This file is the governed ledger for historical bugs discovered during the
2026-05-27 QHH MVP production-like E2E pass. It records current triage truth;
it is not a work queue by itself and this governance pass does not fix the
bugs.

## Ledger Fields

Each governed entry uses these machine-verifiable fields:

- `status`: one of `open`, `resolved`, `superseded`, `stale-needs-repro`, or
  `archived`.
- `owner_area`: one of `compute_control`, `display_readonly`, `slurm_gateway`,
  or `shared_contract`.
- `github_issue`: GitHub issue for an open item when one exists, or `none`.
- `resolved_by`: PR, issue, receipt, commit, or source contract that resolved a
  `resolved` entry.
- `superseded_by`: PR, issue, receipt, runbook, or source contract that
  superseded a `superseded` entry.
- `evidence`: artifact paths, source paths, tests, docs, issues, or PRs backing
  the status.
- `retest_command`: concrete command or explicit live receipt needed to check
  the entry again.

Status meanings:

- `open`: still a known actionable defect or release blocker in the current
  contract.
- `resolved`: current source, tests, docs, or live receipts show the defect is
  fixed.
- `superseded`: the old claim or acceptance path is no longer the governed
  contract; the replacement contract is named.
- `stale-needs-repro`: old evidence is plausible but current source or later
  milestones changed enough that a fresh reproduction is required before
  claiming open or resolved.
- `archived`: retained only for historical traceability.

## 2026-05-27 QHH MVP Production-Like E2E

Original evidence root:
`artifacts/mvp-e2e/qhh-mvp-e2e-20260527T004907Z/`.

Original summary:
`artifacts/mvp-e2e/qhh-mvp-e2e-20260527T004907Z/summary.md`.

Original environment:
`http://127.0.0.1:8001`, DB
`postgresql://nhms:****@10.0.2.100:55432/nhms`.

### BUG-20260527-000: Local repository was stale, hiding the E2E checklist

```yaml
status: resolved
owner_area: shared_contract
github_issue: none
resolved_by: commit 42e70188df881f971bfb78ece65dea9484c1ec01 added the checklist and the local branch was synchronized.
evidence:
  - docs/runbooks/qhh-mvp-production-like-e2e-checklist.md
retest_command: >-
  git cat-file -e 42e70188df881f971bfb78ece65dea9484c1ec01 &&
  test -f docs/runbooks/qhh-mvp-production-like-e2e-checklist.md
```

The root cause was local branch lag, not a missing source document.

### BUG-20260527-001: `psql` CLI was missing for checklist DB commands

```yaml
status: open
owner_area: shared_contract
github_issue: none
evidence:
  - artifacts/mvp-e2e/qhh-mvp-e2e-20260527T004907Z/environment_cli_check.log
  - artifacts/mvp-e2e/qhh-mvp-e2e-20260527T004907Z/db/db_python_preflight.log
retest_command: >-
  command -v psql && psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -c
  "select current_database(), current_user, now();"
```

The Python/SQLAlchemy probe proved DB connectivity, but the checklist still
depends on a system PostgreSQL client unless the DB probe is rewritten into a
repository-managed `uv run` command.

### BUG-20260527-002: Checklist queried `alembic_version`, but the repo uses `schema_migrations`

```yaml
status: open
owner_area: shared_contract
github_issue: none
evidence:
  - artifacts/mvp-e2e/qhh-mvp-e2e-20260527T004907Z/db/db_python_preflight.log
  - packages/common/migrate.py
  - tests/integration_helpers.py
  - tests/test_real_database_integration.py
retest_command: >-
  psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -c
  "select version from public.schema_migrations order by version;"
```

The current migration receipt source is `public.schema_migrations`, not
Alembic's `alembic_version`.

### BUG-20260527-003: Checklist SQL used `bv.basin_id='qhh'`, but QHH basin id is `basins_qhh`

```yaml
status: open
owner_area: shared_contract
github_issue: none
evidence:
  - artifacts/mvp-e2e/qhh-mvp-e2e-20260527T004907Z/db/qhh_baseline.log
  - artifacts/mvp-e2e/qhh-mvp-e2e-20260527T004907Z/db/qhh_schema_aware_counts.log
  - packages/common/forecast_store.py
  - docs/runbooks/qhh-mvp-production-like-e2e-checklist.md
retest_command: |-
  set -euo pipefail
  ! rg -n "bv\\.basin_id = 'qhh'" docs/runbooks/qhh-mvp-production-like-e2e-checklist.md
  rg -n "basins_qhh" docs/runbooks/qhh-mvp-production-like-e2e-checklist.md
  psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -c
    "select count(*) from core.basin_version bv where bv.basin_id = 'basins_qhh';"
```

`packages/common/forecast_store.py` keeps `QHH_BASIN_ID = "basins_qhh"`.
The current checklist still contains the old `qhh` filter in section 7.3, so
the ledger keeps this open rather than over-claiming a docs fix.

### BUG-20260527-004: Unfiltered `plan-production --dry-run` selected non-QHH models

```yaml
status: open
owner_area: compute_control
github_issue: none
evidence:
  - artifacts/mvp-e2e/qhh-mvp-e2e-20260527T004907Z/scheduler_dry_run_compact_summary.json
  - services/orchestrator/cli.py
  - services/orchestrator/scheduler.py
retest_command: >-
  uv run nhms-pipeline plan-production --dry-run --source gfs
  --model-id basins_qhh_shud --basin-id basins_qhh
```

The production scheduler discovers all active models unless model and basin
filters are explicitly provided.

### BUG-20260527-005: `plan-production --dry-run` had download side effects

```yaml
status: open
owner_area: compute_control
github_issue: none
evidence:
  - artifacts/mvp-e2e/qhh-mvp-e2e-20260527T004907Z/scheduler_dry_run.log
  - artifacts/mvp-e2e/qhh-mvp-e2e-20260527T004907Z/scheduler_dry_run_compact_summary.json
  - services/orchestrator/scheduler.py
retest_command: >-
  uv run nhms-pipeline plan-production --dry-run --source gfs
  --model-id basins_qhh_shud --basin-id basins_qhh 2>&1 |
  tee /tmp/nhms-dry-run.log && ! rg -n "download|Downloading|GRIB"
  /tmp/nhms-dry-run.log
```

The old no-mutation proof covered execution candidates, not source discovery
download/cache behavior.

### BUG-20260527-006: QHH-only `plan-production` hit Slurm Gateway HTTP 404

```yaml
status: stale-needs-repro
owner_area: slurm_gateway
github_issue: none
evidence:
  - artifacts/mvp-e2e/qhh-mvp-e2e-20260527T004907Z/scheduler_plan.log
  - artifacts/mvp-e2e/qhh-mvp-e2e-20260527T004907Z/scheduler_plan_compact_summary.json
  - artifacts/mvp-e2e/qhh-mvp-e2e-20260527T004907Z/slurm/slurm_smoke_sacct.log
  - https://github.com/DankerMu/SHUD-NWM/issues/288
  - openspec/changes/m24-multibasin-continuous-daemon-live/issue-292-worklog.md
retest_command: >-
  curl -fsS "$SLURM_GATEWAY_URL/api/v1/slurm/health" &&
  uv run nhms-pipeline plan-production --submit --source gfs
  --model-id basins_qhh_shud --basin-id basins_qhh --max-candidates 1
```

M24 later records node-22 gateway health and generic scheduler receipts, but
this exact 2026-05-27 404 path needs a fresh same-URL submit probe before being
marked resolved.

### BUG-20260527-007: `/api/v1/mvp/qhh/latest-product` was unavailable

```yaml
status: resolved
owner_area: display_readonly
github_issue: none
resolved_by: "#291 plus the M26 node-27 live receipt"
evidence:
  - artifacts/mvp-e2e/qhh-mvp-e2e-20260527T004907Z/api/api_latest_product_gfs.json
  - artifacts/mvp-e2e/qhh-mvp-e2e-20260527T004907Z/api/api_latest_product_ifs.json
  - openspec/changes/m24-multibasin-continuous-daemon-live/issue-291-worklog.md
  - openspec/changes/m26-unified-map-display/worklogs/node27-live-receipt.md
  - packages/common/forecast_store.py
  - tests/test_basins_registry_import.py
  - tests/test_production_scheduler.py
retest_command: >-
  curl -fsS "$API_BASE_URL/api/v1/mvp/qhh/latest-product?source=GFS" |
  jq -e '.data.basin_id == "basins_qhh" and .data.segment_count == 1633'
```

The original failure came from treating geometry segment count as the expected
SHUD output count. #291 split and propagated `output_segment_count`; the M26
node-27 live receipt shows QHH GFS latest-product ready with `segment_count`
1633.

### BUG-20260527-008: QHH segment counts diverged across assets, scheduler, and results

```yaml
status: resolved
owner_area: compute_control
github_issue: none
resolved_by: "#291 M24 multibasin identity/output-segment work"
evidence:
  - artifacts/mvp-e2e/qhh-mvp-e2e-20260527T004907Z/db/qhh_schema_aware_counts.log
  - artifacts/mvp-e2e/qhh-mvp-e2e-20260527T004907Z/db/coverage_probe.log
  - openspec/changes/m24-multibasin-continuous-daemon-live/issue-291-worklog.md
  - docs/runbooks/qhh-22-business-bringup.md
  - workers/model_registry/qhh_production_bootstrap.py
  - tests/test_qhh_production_bootstrap.py
  - tests/test_basins_registry_import.py
  - tests/test_production_scheduler.py
retest_command: >-
  uv run pytest -q tests/test_qhh_production_bootstrap.py
  tests/test_basins_registry_import.py tests/test_production_scheduler.py
  -k output_segment_count
```

The current contract distinguishes GIS geometry segments from SHUD output river
segments. QHH display/output readiness is governed by `.sp.riv`
`output_segment_count=1633`, while GIS river assets can remain a different
count without blocking latest-product.

### BUG-20260527-009: `/ops` source/cycle jobs and stages disagreed with persisted jobs

```yaml
status: superseded
owner_area: shared_contract
github_issue: none
superseded_by: "#233 and M22 strict source/cycle/run/model ops identity"
evidence:
  - artifacts/mvp-e2e/qhh-mvp-e2e-20260527T004907Z/api/api_jobs_gfs_00.json
  - artifacts/mvp-e2e/qhh-mvp-e2e-20260527T004907Z/api/api_pipeline_stages_gfs_00.json
  - https://github.com/DankerMu/SHUD-NWM/issues/233
  - openspec/changes/m22-two-node-docker-readonly-display/tasks.md
  - openspec/changes/m22-two-node-docker-readonly-display/design.md
  - apps/api/routes/pipeline.py
  - tests/test_monitoring_api.py
  - docs/runbooks/two-node-production-e2e-plan.md
retest_command: >-
  curl -fsS "$API_BASE_URL/api/v1/pipeline/stages?source=GFS&cycle_time=$CYCLE_TIME&run_id=$RUN_ID&model_id=basins_qhh_shud"
  && curl -fsS "$API_BASE_URL/api/v1/jobs?source=GFS&cycle_time=$CYCLE_TIME&run_id=$RUN_ID&model_id=basins_qhh_shud&limit=20"
```

The old source/cycle-only historical query is no longer the acceptance path for
cross-plane proof. Current runbooks require strict identity, and the API now
returns mismatch/not-found contracts.

### BUG-20260527-010: Existing jobs lacked readable logs

```yaml
status: stale-needs-repro
owner_area: display_readonly
github_issue: none
evidence:
  - artifacts/mvp-e2e/qhh-mvp-e2e-20260527T004907Z/api/api_job_logs_known_frequency.json
  - artifacts/mvp-e2e/qhh-mvp-e2e-20260527T004907Z/browser/ops-scheduler-failed.after-log.snapshot.txt
  - openspec/changes/m22-two-node-docker-readonly-display/tasks.md
  - openspec/changes/m22-two-node-docker-readonly-display/specs/published-artifact-log-reader/spec.md
  - apps/api/routes/pipeline.py
  - services/artifacts/reader.py
  - tests/test_pipeline_logs_artifacts.py
  - tests/test_monitoring_api.py
retest_command: |-
  set -euo pipefail
  tmp="$(mktemp)"
  status="$(curl -sS -o "$tmp" -w '%{http_code}'
    "$API_BASE_URL/api/v1/jobs/$JOB_ID/logs?source=GFS&cycle_time=$CYCLE_TIME&run_id=$RUN_ID&model_id=basins_qhh_shud")"
  jq -e --arg status "$status" --arg job_id "$JOB_ID" '
    ($status == "200" and .status == "ok" and .data.job_id == $job_id and (.data.content | type == "string"))
    or
    ($status == "404" and .status == "error" and (.error.code | IN("JOB_LOG_NOT_PUBLISHED", "JOB_LOG_NOT_FOUND", "JOB_NOT_FOUND")))
  ' "$tmp"
```

M22 replaced local-path log reading with `ArtifactReader` and stable published
artifact errors such as `JOB_LOG_NOT_PUBLISHED`. Compute-side log publication
remains a dependency, but this entry is owned by node-27 display log
consumption and the published artifact reader; whether all current production
jobs write display-readable log URIs still requires a fresh strict-identity
job/log receipt.

### BUG-20260527-011: Operator cancel of a missing run returned 200 with an empty result

```yaml
status: open
owner_area: compute_control
github_issue: none
evidence:
  - artifacts/mvp-e2e/qhh-mvp-e2e-20260527T004907Z/api/neg_cancel_missing_operator.json
  - artifacts/mvp-e2e/qhh-mvp-e2e-20260527T004907Z/api/neg_cancel_missing_viewer.json
  - apps/api/routes/pipeline.py
  - tests/test_monitoring_api.py
  - tests/test_retry_cancel_consistency.py
retest_command: |-
  set -euo pipefail
  tmp="$(mktemp)"
  status="$(curl -sS -o "$tmp" -w '%{http_code}' -X POST
    "$API_BASE_URL/api/v1/runs/run_missing/cancel" -H "X-User-Role: operator")"
  test "$status" != "200"
  jq -e --arg status "$status" '
    .status == "error"
    and ($status | test("^(404|409|422)$"))
    and (.error.code | type == "string")
  ' "$tmp"
```

The compute-control cancel route still starts from active jobs for a run. The
fixed state is that operator cancel for a missing run returns a stable non-200
error body, not HTTP 200 with empty `cancelled_jobs`. The display-readonly route
correctly fails closed as manual action, but the operator missing-run semantics
are not yet fixed by that display safety work.

### BUG-20260527-012: Default Playwright E2E specs were mocked regression, not live E2E

```yaml
status: superseded
owner_area: display_readonly
github_issue: none
superseded_by: "#365 Governance-2D mocked-vs-live Playwright split"
evidence:
  - artifacts/mvp-e2e/qhh-mvp-e2e-20260527T004907Z/frontend/e2e_specs_mocked_detection.log
  - https://github.com/DankerMu/SHUD-NWM/issues/365
  - docs/VALIDATION.md
  - docs/governance/LEGACY_DEAD_CODE_INVENTORY.md
  - apps/frontend/playwright.config.ts
  - apps/frontend/playwright.live-display.config.ts
  - apps/frontend/e2e/live-display.spec.ts
  - apps/frontend/src/__tests__/playwrightConfig.test.ts
retest_command: >-
  cd apps/frontend && corepack pnpm run test:e2e:live-display -- --list
```

The mocked specs are now explicitly named `mocked-regression-chromium`.
The live-display profile requires `PLAYWRIGHT_LIVE_BASE_URL` and
`PLAYWRIGHT_LIVE_API_BASE_URL`, rejects username/password userinfo, and has a
static guard against broad `page.route('**/api/v1/**')` mocks in live specs.
Current local live runtime remains `BLOCKED` when those env vars or a real
display_readonly runtime are absent; that is not a live PASS.

### BUG-20260527-013: Retry/cancel produced mock Slurm job IDs, not live Slurm receipts

```yaml
status: open
owner_area: slurm_gateway
github_issue: none
evidence:
  - artifacts/mvp-e2e/qhh-mvp-e2e-20260527T004907Z/api/api_retry_cycle_gfs_2026052618_operator.json
  - artifacts/mvp-e2e/qhh-mvp-e2e-20260527T004907Z/api/api_cancel_cycle_gfs_2026052618_operator.json
  - artifacts/mvp-e2e/qhh-mvp-e2e-20260527T004907Z/db/final_pipeline_jobs_snapshot.log
  - services/slurm_gateway/config.py
  - services/slurm_gateway/gateway.py
  - services/slurm_gateway/mock_backend.py
  - tests/test_real_slurm_gateway.py
  - tests/test_monitoring_api.py
retest_command: |-
  # Preconditions: gateway and API are already running against a real Slurm backend.
  set -euo pipefail
  curl -fsS "$SLURM_GATEWAY_URL/api/v1/slurm/health" |
    jq -e '(.backend? // "slurm") == "slurm" and (.healthy? // true) == true'
  job_id="$(curl -fsS -X POST "$API_BASE_URL/api/v1/runs/$RUN_ID/retry"
    -H "X-User-Role: operator" |
    jq -er '.data.slurm_job_id | select(test("^[0-9]+(_[0-9]+)?$"))')"
  curl -fsS "$SLURM_GATEWAY_URL/api/v1/slurm/jobs/$job_id" |
    jq -e --arg job_id "$job_id" '.job_id == $job_id'
  sacct -j "$job_id" --noheader --format=JobID,State | rg -n "$job_id"
```

`services/slurm_gateway/config.py` still defaults the gateway backend to
`mock`. A live retry receipt must use the real Slurm backend, show an
accounting-visible Slurm job id, and write back job/status/log/accounting.

### BUG-20260527-014: `/ops` browser feedback for logs and retry was incomplete

```yaml
status: open
owner_area: display_readonly
github_issue: https://github.com/DankerMu/SHUD-NWM/issues/386
evidence:
  - artifacts/mvp-e2e/qhh-mvp-e2e-20260527T004907Z/browser/ops-scheduler-failed.snapshot.txt
  - artifacts/mvp-e2e/qhh-mvp-e2e-20260527T004907Z/browser/ops-scheduler-failed.after-log.snapshot.txt
  - artifacts/mvp-e2e/qhh-mvp-e2e-20260527T004907Z/browser/ops-scheduler-failed.after-retry.snapshot.txt
  - artifacts/mvp-e2e/qhh-mvp-e2e-20260527T004907Z/screenshots/ops-scheduler-failed-after-retry.png
  - apps/frontend/src/components/monitoring/JobsTable.tsx
  - apps/frontend/src/stores/monitoring.ts
retest_command: >-
  cd apps/frontend && PLAYWRIGHT_LIVE_BASE_URL="$DISPLAY_URL"
  PLAYWRIGHT_LIVE_API_BASE_URL="$API_BASE_URL"
  corepack pnpm run test:e2e:live-display -- --grep "@ops"
```

Open follow-up #386 covers stale failed-stage evidence after successful manual
retry. The browser still needs a live display_readonly receipt showing stable
log unavailable feedback and no hidden retry/cancel mutation from node-27.
