# Bugs Governance Ledger

This file is the governed ledger for historical bugs discovered during the
2026-05-27 QHH MVP production-like E2E pass. It records current triage truth;
it is not a work queue by itself and this governance pass does not fix the
bugs.

**Closed to new entries (#3226, 2026-11-19).** New defects are filed as GitHub
issues, not added here. Every entry left `open` names the GitHub issue that
owns it; an entry closes when that issue's PR lands. The file stays as the
historical record that runbooks, receipts and ADRs link to.

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
status: superseded
owner_area: shared_contract
github_issue: none
superseded_by: >-
  The checklist is frozen as historical M21 evidence (its header, M26 single-map
  authority in docs/governance/DOC_STATUS.md). Current node-27 DB probes run the
  client inside the database container (`docker exec nhms-db psql ...`, see
  docs/runbooks/tier-node27-timeseries-storage.md), so no host `psql` is needed.
evidence:
  - docs/runbooks/qhh-mvp-production-like-e2e-checklist.md
  - docs/runbooks/tier-node27-timeseries-storage.md
  - artifacts/mvp-e2e/qhh-mvp-e2e-20260527T004907Z/environment_cli_check.log
  - artifacts/mvp-e2e/qhh-mvp-e2e-20260527T004907Z/db/db_python_preflight.log
retest_command: >-
  command -v psql && psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -c
  "select current_database(), current_user, now();"
```

The Python/SQLAlchemy probe proved DB connectivity. The checklist's `psql`
commands are kept as the 2026-05-27 record; live probes today go through the
database container's own client (#3226 triage).

### BUG-20260527-002: Checklist queried `alembic_version`, but the repo uses `schema_migrations`

```yaml
status: resolved
owner_area: shared_contract
github_issue: none
resolved_by: >-
  No checklist command queries `alembic_version` any more; the only mention is
  the 2026-05-27 status-table row that records this bug. The migration receipt
  is `public.schema_migrations` (packages/common/migrate.py). Verified in #3226.
evidence:
  - artifacts/mvp-e2e/qhh-mvp-e2e-20260527T004907Z/db/db_python_preflight.log
  - docs/runbooks/qhh-mvp-production-like-e2e-checklist.md
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
status: resolved
owner_area: shared_contract
github_issue: https://github.com/DankerMu/SHUD-NWM/issues/3226
resolved_by: >-
  #3226 changed the two section 7.3 queries of
  docs/runbooks/qhh-mvp-production-like-e2e-checklist.md to
  `bv.basin_id = 'basins_qhh'`.
evidence:
  - artifacts/mvp-e2e/qhh-mvp-e2e-20260527T004907Z/db/qhh_baseline.log
  - artifacts/mvp-e2e/qhh-mvp-e2e-20260527T004907Z/db/qhh_schema_aware_counts.log
  - packages/common/forecast_store.py
  - docs/runbooks/qhh-mvp-production-like-e2e-checklist.md
retest_command: |-
  set -euo pipefail
  ! rg -n "bv\\.basin_id = 'qhh'" docs/runbooks/qhh-mvp-production-like-e2e-checklist.md
  rg -n "basins_qhh" docs/runbooks/qhh-mvp-production-like-e2e-checklist.md
  psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -Atq -c \
    "select exists(select 1 from core.basin_version bv where bv.basin_id = 'basins_qhh');" | grep -qx t
```

`packages/common/forecast_store.py` keeps `QHH_BASIN_ID = "basins_qhh"`.
Section 7.3 of the checklist now filters on that id (#3226).

### BUG-20260527-004: Unfiltered `plan-production --dry-run` selected non-QHH models

```yaml
status: superseded
owner_area: compute_control
github_issue: none
superseded_by: >-
  Not a defect: an unfiltered `plan-production` plans every active model by
  design, and production planning is the node-22 scheduler timer, which is
  meant to cover all active models. A QHH-only dry run passes `--model-id` and
  `--basin-id` (the retest below). Triage in #3226.
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
status: resolved
owner_area: compute_control
github_issue: none
resolved_by: >-
  In DB-free mode (`NHMS_SCHEDULER_DB_FREE_REQUIRED=true`, the only mode the
  node-22 production scheduler runs) source discovery reads the NFS raw
  manifest and downloads nothing
  (services/orchestrator/scheduler_core.py adapter selection ->
  services/orchestrator/scheduler_adapters.py `_db_free_default_adapters` /
  `_readiness` -> services/orchestrator/source_cycle_raw_manifest.py
  `nfs_raw_manifest_readiness_from_env`). Verified by code reading in #3226.
evidence:
  - artifacts/mvp-e2e/qhh-mvp-e2e-20260527T004907Z/scheduler_dry_run.log
  - artifacts/mvp-e2e/qhh-mvp-e2e-20260527T004907Z/scheduler_dry_run_compact_summary.json
  - services/orchestrator/scheduler_adapters.py
  - services/orchestrator/source_cycle_raw_manifest.py
retest_command: |-
  set -euo pipefail
  dry_run_log="$(mktemp)"
  NHMS_SCHEDULER_DB_FREE_REQUIRED=true uv run nhms-pipeline plan-production --dry-run --source gfs \
    --model-id basins_qhh_shud --basin-id basins_qhh 2>&1 | tee "$dry_run_log"
  ! rg -n "download|Downloading|GRIB" "$dry_run_log"
```

The old no-mutation proof covered execution candidates, not source discovery
download/cache behavior. In DB-free mode discovery only reads manifests written
by the download lane, so a dry run has nothing to download. The non-DB-free
mode used the worker GFS/IFS adapters through the scheduler's SQL default-adapter
factory, whose availability probe contacts the upstream source; #3255 (R2b) made
DB-free the only scheduler mode and #3256 (R2c-1) deleted that factory, so no
scheduler pass builds those adapters any more.

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
retest_command: |-
  set -euo pipefail
  uv run pytest -q \
    tests/test_qhh_production_bootstrap.py \
    tests/test_basins_registry_import.py \
    tests/test_production_scheduler.py \
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
retest_command: |-
  set -euo pipefail
  tmp="$(mktemp -d)"
  stages="$tmp/stages.json"
  jobs="$tmp/jobs.json"
  stages_status="$(curl -sS -o "$stages" -w '%{http_code}' \
    "$API_BASE_URL/api/v1/pipeline/stages?source=GFS&cycle_time=$CYCLE_TIME&run_id=$RUN_ID&model_id=basins_qhh_shud")"
  jobs_status="$(curl -sS -o "$jobs" -w '%{http_code}' \
    "$API_BASE_URL/api/v1/jobs?source=GFS&cycle_time=$CYCLE_TIME&run_id=$RUN_ID&model_id=basins_qhh_shud&limit=20")"
  jq -e --arg status "$stages_status" --arg run_id "$RUN_ID" --arg model_id basins_qhh_shud '
    $status == "200" and .status == "ok" and (.data | type == "array")
    and ([.data[].basin_results[]?] | length > 0)
    and all(.data[].basin_results[]?; .run_id == $run_id and .model_id == $model_id)
  ' "$stages"
  jq -e --arg status "$jobs_status" --arg run_id "$RUN_ID" --arg model_id basins_qhh_shud '
    $status == "200" and .status == "ok" and (.data.items | type == "array")
    and (.data.total > 0)
    and all(.data.items[]; .run_id == $run_id and .model_id == $model_id)
  ' "$jobs"
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
  # JOB_ID must name an existing strict-identity job for the source/cycle/run/model tuple.
  tmp="$(mktemp)"
  status="$(curl -sS -o "$tmp" -w '%{http_code}' \
    "$API_BASE_URL/api/v1/jobs/$JOB_ID/logs?source=GFS&cycle_time=$CYCLE_TIME&run_id=$RUN_ID&model_id=basins_qhh_shud")"
  jq -e --arg status "$status" --arg job_id "$JOB_ID" '
    ($status == "200" and .status == "ok" and .data.job_id == $job_id and (.data.content | type == "string"))
    or
    ($status == "404" and .status == "error" and (.error.code | IN("JOB_LOG_NOT_PUBLISHED", "JOB_LOG_NOT_FOUND")))
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
github_issue: https://github.com/DankerMu/SHUD-NWM/issues/3245
evidence:
  - artifacts/mvp-e2e/qhh-mvp-e2e-20260527T004907Z/api/neg_cancel_missing_operator.json
  - artifacts/mvp-e2e/qhh-mvp-e2e-20260527T004907Z/api/neg_cancel_missing_viewer.json
  - apps/api/routes/pipeline.py
  - tests/test_monitoring_api.py
  - tests/test_retry_cancel_consistency.py
retest_command: |-
  set -euo pipefail
  tmp="$(mktemp)"
  status="$(curl -sS -o "$tmp" -w '%{http_code}' -X POST \
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
are not yet fixed by that display safety work. In #3226 the 200 path was
confirmed in code (`apps/api/routes/pipeline.py` `cancel_run` returns `_ok`
when no job exists), but it is reachable only on a DB-backed compute API:
production node-22 runs its compute API without `DATABASE_URL`, so an
authorized cancel there fails at `get_pipeline_store` with 500
`DATABASE_URL_MISSING`, and node-27 answers the manual-action 409. #3245 owns
the decision (retire the route like retry, or return 404).

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
  - apps/frontend/playwright.live-river-click.config.ts
  - apps/frontend/playwright.live-c4-display.config.ts
  - apps/frontend/e2e/live-display.spec.ts
  - apps/frontend/e2e/live-c4-display.spec.ts
  - apps/frontend/src/__tests__/playwrightConfig.test.ts
retest_command: >-
  cd apps/frontend && corepack pnpm test -- playwrightConfig.test.ts
```

The mocked specs are now explicitly named `mocked-regression-chromium`.
The live profiles (`live-river-click` and `live-c4-display`) require
`PLAYWRIGHT_LIVE_BASE_URL` and `PLAYWRIGHT_LIVE_API_BASE_URL`, reject
username/password userinfo, and have static guards against broad
`page.route('**/api/v1/**')` mocks in live specs. Current local live runtime
remains `BLOCKED` when those env vars or a real display_readonly runtime are
absent; that is not a live PASS. The original two-URL `/monitoring` live
profile was retired by #3202; its no-broad-mock guard now runs on the
river-click config.

### BUG-20260527-013: Retry/cancel produced mock Slurm job IDs, not live Slurm receipts

```yaml
status: resolved
owner_area: slurm_gateway
github_issue: https://github.com/DankerMu/SHUD-NWM/issues/3246
resolved_by: >-
  #3246: the scheduler's HTTP gateway probe refuses a gateway whose health body
  names a backend other than `slurm` (unhealthy, not submit-capable,
  `SLURM_GATEWAY_UNAVAILABLE` with a reason naming the reported backend), so no
  pass submits through a gateway on the mock backend; `real` is no longer a
  backend name anywhere in the scheduler.
evidence:
  - services/orchestrator/scheduler_preflight.py
  - services/orchestrator/scheduler_gateway.py
  - tests/test_production_scheduler.py
  - docs/runbooks/slurm-gateway-preflight.md
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
  # Env: RUN_ID (fcst_<source>_<cycle>_<model>), SOURCE, CYCLE_TIME, MODEL_ID (the strict
  # identity GET /api/v1/jobs requires once run_id is given), PREV_JOB_ID (the failed job),
  # JOURNAL_ROOT and OPERATOR (node-22 marker lane).
  set -euo pipefail
  curl -fsS "$SLURM_GATEWAY_URL/api/v1/slurm/health" |
    jq -e '(.backend? // "slurm") == "slurm" and (.healthy? // true) == true'
  # The API retry route is retired (#3157, always 409 CONTROL_PLANE_MANUAL_ACTION_REQUIRED).
  # On node-22, mark the run; the next scheduler pass mints and submits the retry.
  cd /scratch/frd_muziyao/NWM
  .venv/bin/python scripts/node22_manual_retry_failed_runs.py \
    --journal-root "$JOURNAL_ROOT" --run-id "$RUN_ID" \
    --reason "BUG-20260527-013 retest" --requested-by "$OPERATOR" --execute
  # Wait (up to 6h) for a retry job other than the failed one to carry a live Slurm id.
  job_id=""
  for _ in $(seq 1 72); do
    job_id="$(curl -fsS -G "$API_BASE_URL/api/v1/jobs" \
      --data-urlencode "source=$SOURCE" --data-urlencode "cycle_time=$CYCLE_TIME" \
      --data-urlencode "run_id=$RUN_ID" --data-urlencode "model_id=$MODEL_ID" \
      --data-urlencode "sort_by=submitted_at" --data-urlencode "sort_order=desc" \
      --data-urlencode "limit=50" |
      jq -r --arg prev "$PREV_JOB_ID" '[.data.items[]
        | select(.job_id != $prev and (.retry_count // 0) > 0)
        | .slurm_job_id | select(. != null and test("^[0-9]+(_[0-9]+)?$"))] | first // empty')"
    [ -n "$job_id" ] && break
    sleep 300
  done
  [ -n "$job_id" ]
  curl -fsS "$SLURM_GATEWAY_URL/api/v1/slurm/jobs/$job_id" |
    jq -e --arg job_id "$job_id" '.job_id == $job_id'
  sacct -j "$job_id" --noheader --format=JobID,State | rg -n "$job_id"
```

`services/slurm_gateway/config.py` still defaults the gateway backend to
`mock`. A live retry receipt must use the real Slurm backend, show an
accounting-visible Slurm job id, and write back job/status/log/accounting.

#3246 chose the guard at the scheduler's probe, not a refusal at gateway
start-up: a scheduler configured for the `slurm` backend treats a remote
gateway whose health body names any other backend as unhealthy and not
submit-capable, and the pass carries `SLURM_GATEWAY_UNAVAILABLE` with the
reported backend in the reason. A health body without a backend name is judged
on health and binaries as before, and the in-process mock probe used in
development and tests is unchanged. The backend vocabulary is `mock` and
`slurm`; a scheduler configured with `real` (or any other name) takes the
in-process path, where `create_gateway` rejects the name, so its gateway check
is unhealthy. Known limits: a scheduler whose own environment loses
`SLURM_GATEWAY_BACKEND` takes the in-process path and never asks the remote
gateway (it loses the HTTP probe and the cohort split, while its submissions
still go to the gateway URL); the gateway still starts on the mock backend when
its environment says nothing, and the scheduler no longer submits through it;
the control API can still mount the gateway routes in-process on the mock
backend in development.

### BUG-20260527-014: `/ops` browser feedback for logs and retry was incomplete

```yaml
status: superseded
owner_area: display_readonly
github_issue: https://github.com/DankerMu/SHUD-NWM/issues/386
superseded_by: >-
  #3179 removed the retry control from the /ops jobs table (retry is the
  node-22 manual-repair marker lane; `POST .../retry` is a 409 stub), and #386
  is closed. Log-unavailable feedback is checked by the C4 `/ops` live lane
  below. Triage in #3226.
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
  PLAYWRIGHT_LIVE_C4_BASIN_ID="$C4_BASIN_ID"
  PLAYWRIGHT_LIVE_C4_SEGMENT_ID="$C4_SEGMENT_ID"
  PLAYWRIGHT_LIVE_C4_RECEIPT_PATH="$C4_RECEIPT_PATH"
  corepack pnpm run test:e2e:live-c4-display
```

Issue #386 (stale failed-stage evidence after a successful manual retry) is closed,
and the page no longer has a retry control to give feedback for (#3179).
A re-check is a live display_readonly receipt showing stable
log unavailable feedback and no hidden retry/cancel mutation from node-27.
The retest is the C4 `/ops` check, because the earlier two-URL live lane was
retired by #3202.

## 2026-06-20 Display bootstrap decoupling (Epic #579)

### BUG-20260620-001: `/api/v1/layers` cold path ran 21+ seconds because of dead `water-level` variant + frontend `flood_product_ready=true` hardcode

```yaml
status: resolved
owner_area: display_readonly
github_issue: https://github.com/DankerMu/SHUD-NWM/issues/579
resolved_by: >-
  Epic #579 split into PR 1/7..PR 5/7 (#587 backend dead-variant + #588 frontend
  dead-variant + #589 loading split + #590 dead-call removal + #591 discharge
  decoupling); PR 6/7 (#585) live receipt on node-27 captures the 21,430 ms
  warm → 413 ms cold proof at SHA 122ea95.
evidence:
  - docs/runbooks/receipts/display-bootstrap-decoupling-20260620.md
  - scripts/diagnostic/display-cold-waterfall.sh
  - openspec/changes/refactor-display-overview-bootstrap/issue-585-worklog.md
  - openspec/changes/refactor-display-overview-bootstrap/specs/overview-data-contracts/spec.md
  - openspec/changes/refactor-display-overview-bootstrap/design.md
retest_command: >-
  ssh -p 32099 nwm@210.77.77.27
  'cd /home/nwm/NWM && git pull --ff-only && bash scripts/diagnostic/display-cold-waterfall.sh --runs 3'
```

Root cause was a `code-contract` defect with two coupled vectors: (a) the
`water-level` MVT variant remained in the backend layer catalog and SQL
path even though no frontend consumer rendered it — every catalog request
fanned out the dead variant's `hydro.river_timeseries` SkipScan (92M rows),
contributing the majority of the 21.8 s cold tail; (b) the frontend hardcoded
`flood_product_ready=true` on every `/api/v1/runs` request including discharge,
turning a non-blocking enrichment gate into a blocking 12 s aggregation on the
default user path. Receipt at SHA `122ea95` shows the pre-merge 21,430 ms warm
baseline reproduced live and the post-merge cold median of 413 ms (51.9×
speedup); steady-state warm 2–3 ms.

PR 5/7's live spec proof recorded in the receipt: GFS discharge has 142 runs
in the current node-27 DB, ALL `frequency_done: unavailable` — without the PR
the frontend's filter would have returned 0 runs and the discharge layer would
have been broken end-to-end. The bug is therefore not just a latency one but a
spec-faithfulness one: the prior code mis-served "discharge MUST be independent
of flood readiness" as "discharge requires flood readiness", breaking the
default user path silently when no flood-ready runs existed.

Cold-cache fidelity note: receipt is `Python-LRU-cold + Postgres-buffer-warm`
(Postgres restart needs sudo, out of scope). The 51.9× delta is real
code-path improvement, not buffer-cache artifact — both pre- and post-merge
were measured against the same Postgres buffer state.

Carried follow-ups (recorded in receipt, NOT this bug entry):
- 413 ms exceeds spec's 200 ms cold floor by ~213 ms; mitigation tracked
  in follow-up issue #593 (uvicorn `--preload`, sqlalchemy pool pre-ping,
  or spec amendment).
- Browser PNG capture deferred — operator attested acceptance #2 directly
  after reviewing API-side proof (see receipt Section E); capture procedure
  preserved for future regression replay.
