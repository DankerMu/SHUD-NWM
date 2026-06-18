> 对账回填（2026-06-06）：M22 代码层基本完成，本文 checkbox 此前严重滞后于代码。
> 经 tasks↔代码逐项核实后回填。约定：
> - `[x]` = 代码 + 单测已落地（含 §7–§10 原已勾的 Docker/systemd/E2E 交付）。
> - `[ ] （B-尾巴）` = 功能已实现，仅测试/契约自动化不全，待本地补齐。
> - `[ ] （C-live）` = 代码 + 单测已实现，PASS 证据需 node-27 实机 / 真实只读 DB / 浏览器才能产出。
> 详见 `docs/runbooks/node-27-bringup-checklist.md`。

## 0. Change Preflight and Baseline

- [x] 0.1 Validate this OpenSpec change with `openspec validate m22-two-node-docker-readonly-display --strict --no-interactive` before implementation issues begin. （validate 已由 §7.9 覆盖通过）
- [x] 0.2 Record the current code baseline in the Epic: `apps/api/main.py` unconditionally mounts `slurm_router`, retry/cancel call gateway paths, `/jobs/{job_id}/logs` is local-path oriented, latest-product accepts only `source`, and production Docker assets are limited to `infra/docker-compose.dev.yml`. （已记于 design.md）
- [x] 0.3 Record the rollout invariant in the Epic: safety boundaries land before Docker deployment assets; Docker must not copy control-plane capability to 27. （已记于 design.md）
- [x] 0.4 Record the temporary/evidence path constraint in the Epic and Docker issues: project-created temp, review, Docker smoke, and E2E artifacts go under `artifacts/` or `/scratch/frd_muziyao`. （已记于 design.md）

## 1. Runtime Service Role Boundary

- [x] 1.1 Add a runtime role helper/config for `dev_monolith`, `compute_control`, `display_readonly`, and reserved-or-dedicated `slurm_gateway`. （apps/api/runtime_mode.py:10-15）
- [x] 1.2 Define the production-like predicate: missing role fails when `NHMS_REQUIRE_SERVICE_ROLE=true` or `NHMS_AUTH_MODE=production|live|live_idp`; local/test may default to `dev_monolith` only when those signals are absent. （runtime_mode.py:109-121）
- [x] 1.3 Gate `apps/api/main.py` Slurm router registration by role so `display_readonly` does not register `/api/v1/slurm/*` and display OpenAPI does not advertise those routes. （main.py:310-311 + test_runtime_mode.py）
- [x] 1.4 Add display-role unsafe config detection for Slurm gateway env, compute workspace/Basins/SHUD env, and control mutations. （runtime_mode.py:175-211）
- [x] 1.5 Add `GET /api/v1/runtime/config` or equivalent read-only runtime config endpoint returning service role and capability flags for frontend gating; update OpenAPI and generated frontend types. （main.py:283-286 + openapi:1264-1281 + types.ts:1468-1473）
- [x] 1.6 Define `slurm_gateway` startup semantics: either reserved/fail-fast unless a dedicated gateway app exists, or a dedicated app that exposes only health and `/api/v1/slurm/*`. （services/slurm_gateway/app.py:26-38）
- [x] 1.7 Add focused tests for role parsing, production missing-role failure, display route absence, compute/dev route presence, display unsafe config blockers, runtime config output, and `slurm_gateway` route inventory or reserved-role failure. （tests/test_runtime_mode.py）

## 2. Display Retry/Cancel Fail-Closed Backend

- [x] 2.1 Add a display-mode guard to `POST /api/v1/runs/{run_id}/retry` that returns HTTP `409` with standard error envelope code `CONTROL_PLANE_MANUAL_ACTION_REQUIRED` and safe manual 22 recovery details for otherwise-authorized callers. （routes/pipeline.py:169-234）
- [x] 2.2 Add a display-mode guard to `POST /api/v1/runs/{run_id}/cancel` that returns HTTP `409` with standard error envelope code `CONTROL_PLANE_MANUAL_ACTION_REQUIRED` and safe manual 22 recovery details for otherwise-authorized callers. （routes/pipeline.py:177-182,566-728）
- [x] 2.3 Preserve existing auth/RBAC ordering so unauthenticated or unauthorized display retry/cancel callers receive existing `401` or `403` responses without manual recovery details. （routes/pipeline.py:155-166）
- [x] 2.4 Ensure display retry/cancel does not construct or call `get_slurm_gateway()`, does not submit/cancel jobs, and does not write pipeline/hydro/met terminal state. （tests/test_retry_cancel_consistency.py:742-869 断言无副作用）
- [x] 2.5 Add display-mode `/api/v1/queue/depth` behavior that is DB-derived or returns a stable read-only unavailable error without constructing or calling `get_slurm_gateway()`. （routes/pipeline.py:201-218 返回 503 CONTROL_PLANE_QUEUE_UNAVAILABLE）
- [x] 2.6 Preserve compute-control/dev retry/cancel and queue-depth behavior plus existing RBAC semantics. （compute_control 路径回归测试在 test_retry_cancel_consistency.py）
- [x] 2.7 Update static/runtime OpenAPI, generated frontend types, API contract tests, and OpenAPI drift tests for the manual-action and queue-depth display contracts. （已补：openapi 新增 ControlPlaneManualActionRequired/ControlPlaneQueueUnavailable，retry/cancel 409 + queue 503，drift 测试 test_api_contract.py::test_display_control_plane_responses_have_no_static_runtime_drift）
- [x] 2.8 Add backend tests with gateway spies and DB state assertions for display retry/cancel and queue-depth no-side-effect behavior, auth/RBAC matrix, plus compute-control regression. （已补：test_retry_cancel_consistency.py 新增 401/403 denied + compute_control normal 矩阵，gateway-spy no-write + 终态未变断言）

## 3. Published Artifact Log Reader

- [x] 3.1 Add `services/artifacts` config, URI parsing, redaction, and reader support for `published://`, allowed publish-root `file://`, and allowlisted `s3://` log URIs using canonical env names
  `NHMS_PUBLISHED_ARTIFACT_ROOT`, `NHMS_PUBLISHED_ARTIFACT_URI_PREFIX`, `NHMS_PUBLISHED_ARTIFACT_S3_BUCKET`, and `NHMS_PUBLISHED_ARTIFACT_S3_PREFIX`. （services/artifacts/reader.py:59-176）
- [x] 3.2 Enforce publish-root containment, S3 bucket/prefix allowlist, symlink rejection, path traversal and encoded separator rejection, credential-bearing URI rejection, and `NHMS_LOG_TAIL_MAX_BYTES` tail limits. （reader.py:240,332,620-677,707-716）
- [x] 3.3 Add compute-side log publication or URI normalization so newly recorded `ops.pipeline_job.log_uri` values use supported published artifact URIs, with canonical `published://logs/<source>/<cycle_time>/<run_id>/<job_id>.out|err` where practical. （chain.py:4143-4149 + reader.py:394-411）
- [x] 3.4 Migrate `/api/v1/jobs/{job_id}/logs` to use ArtifactReader and return exact stable errors: `JOB_LOG_NOT_PUBLISHED`, `JOB_LOG_URI_UNSUPPORTED`, `JOB_LOG_ACCESS_DENIED`, and `JOB_LOG_NOT_FOUND`. （routes/pipeline.py:459-502,2288-2319 + reader.py:18-21）
- [x] 3.5 Preserve or explicitly gate legacy local `LOG_ROOT` behavior for `dev_monolith`; ensure `display_readonly` with `NHMS_DISPLAY_ALLOW_LOCAL_FILE_LOGS=false` cannot use private local paths. （reader.py:207-316,707-716 + test_pipeline_logs_artifacts.py:218-241）
- [x] 3.6 Update static/runtime OpenAPI, generated frontend types, API contract tests, and OpenAPI drift tests for log errors and response metadata. （已补：openapi 新增 JobLogError 四码枚举（400/403/404），drift 测试 test_api_contract.py::test_job_log_error_codes_are_declared_in_static_and_runtime_contract）
- [x] 3.7 Add tests for compute log URI emission, published local logs, allowed `file://` logs, mocked allowlisted S3 logs, disallowed S3 bucket/prefix, tail limits, unsafe paths, private workspace paths, credential redaction, unsupported scheme, missing object, and API error mapping. （test_artifact_reader.py(39) + test_pipeline_logs_artifacts.py(19)）

## 4. QHH Latest Product Strict Identity

- [x] 4.1 Extend `GET /api/v1/mvp/qhh/latest-product` query parameters and OpenAPI schema to accept optional `run_id`, `cycle_time`, and `model_id` alongside `source`; if any strict identity field is present, all four strict fields are required for cross-plane proof. （routes/forecast.py:114-251 + openapi:641-668）
- [x] 4.2 Extend `packages/common/forecast_store.py` latest-product selection so strict filters must match exactly and never fall back to historical latest data. （forecast_store.py:1020-1021,1495-1508）
- [ ] 4.3 Ensure ready and unavailable responses include safe identity details for `run_id`, `source_id`, `cycle_time`, `model_id`, basin identity, forcing version, basin version, and river network version where applicable. （C-live：payload 构造已实现并有单测 test_api_contract.py:277-284，端到端 identity 细节待 browser/cross-plane live 确认）
- [x] 4.4 Reject partial strict identity at the backend with `422 VALIDATION_ERROR`, safe missing/provided/required field details, and no source-only store lookup. （forecast.py:217-250 + test_api_contract.py:199-294）
- [x] 4.5 Update generated frontend API types plus API contract and OpenAPI drift tests for the backend query contract. （types.ts:2338-2370 + test_api_contract.py:176-344）
- [x] 4.6 Add backend tests for source-only compatibility, strict match success, strict mismatch unavailable, partial strict identity rejection, duplicate same-source/cycle run/model mismatch, model/source/cycle normalization, no historical fallback, and strict SQL predicates. （test_forecast_api.py:1674+,1803+,1865+ + test_api_contract.py）

Issue ownership note:

- #232 owns the backend latest-product API/store contract, OpenAPI schema, generated frontend type surface, and backend tests for source-only compatibility, strict success/mismatch, partial strict identity rejection, normalization, and no historical fallback.
- #233 owns `/ops` pipeline status/stages/jobs strict identity filtering.
- #235 owns `/hydro-met` frontend bootstrap, frontend strict query behavior, and browser/UI tests.
- #239 owns final cross-plane E2E evidence and pass/fail/partial reporting.

## 4A. Pipeline Ops Strict Run Identity

- [x] 4A.1 Extend pipeline status, stages, jobs, and logs APIs or response metadata so strict consumers can bind `source`, `cycle_time`, `run_id`, and `model_id` together. （routes/pipeline.py:102-113,250-503）
- [x] 4A.2 Preserve existing source/cycle status, stages, jobs, pagination, sorting, status/stage filters, and log behavior for non-strict browsing. （routes/pipeline.py:253-435 + openapi:1134-1135）
- [x] 4A.3 Reject partial strict ops identity with `422 VALIDATION_ERROR`, safe `missing_fields`, `provided_fields`, `required_fields`, and `strict_identity_required=true` before source/cycle-only lookup. （routes/pipeline.py:1306-1354）
- [x] 4A.4 Ensure strict status/stages/jobs evidence resolves to a concrete `hydro.hydro_run` identity and cannot use jobs from a same-source/cycle sibling run or model. （routes/pipeline.py:1474-1527,2036-2070）
- [x] 4A.5 Ensure strict job log evidence validates the requested `run_id` and `model_id` before reading the published log, and wrong-run jobs/logs return HTTP `409` with code `PIPELINE_STRICT_IDENTITY_MISMATCH`. （routes/pipeline.py:487-488 + test_monitoring_api.py:1448-1513）
- [x] 4A.6 Add duplicate same-source/cycle fixtures where two runs differ by `run_id` or `model_id`, including jobs/logs that would otherwise be mixed by source/cycle-only queries. （test_monitoring_api.py:1380-1467）
- [x] 4A.7 Add strict identity error contract tests for `422 VALIDATION_ERROR`, `404 PIPELINE_STRICT_IDENTITY_NOT_FOUND`, and `409 PIPELINE_STRICT_IDENTITY_MISMATCH`, including safe details and log mismatch before artifact read. （test_api_contract.py:390-425 + test_monitoring_api.py:1398-1514）
- [x] 4A.8 Update static/runtime OpenAPI, generated frontend API types, API contract tests, and OpenAPI drift tests for the strict ops identity contract. （openapi:1062-1241 + types.ts:2667-2792 + test_api_contract.py:390-425）

## 5. Readonly DB Boundary

- [ ] 5.1 Add readonly DB validation tests or runbook commands proving display APIs work with readonly credentials for health, models, stations, latest-product, pipeline status, stages, jobs, logs, and runtime config;
  identity-bound route PASS evidence must use one strict `source`/`cycle_time`/`run_id`/`model_id` identity and logs must also bind `job_id`. （C-live：验证逻辑 + 57 测试已实现 test_readonly_db_validation.py:1165-1254，但 PASS receipt 需 node-27 真实只读 DB）
- [ ] 5.2 Add readonly DB catalog-first permission inventory and permission-denied probes that record `current_user` and prove controlled `INSERT`, `UPDATE`, `DELETE`, and DDL attempts are rejected before commit
  for hydro, met, ops, and pipeline-critical tables when catalog inventory is clean; table `TRUNCATE`, fail-closed non-read table privileges, column mutating grants, any `hydro`/`met`/`ops` sequence
  `USAGE`/`UPDATE`, schema `CREATE` across all probed schemas, current database `CREATE`, and reachable writer-role membership are `FAIL`. Any catalog mutating capability must prevent `nextval`, `setval`,
  DML, or DDL execution anywhere in the matrix. （C-live：探测矩阵代码已实现 readonly_db_validation.py:192-199 + 全 sim 测试，denied-write receipt 需真实只读凭证）
- [x] 5.3 Ensure display retry/cancel returns manual action before any write attempt when using readonly DB credentials. （readonly_db_validation.py:1891-1944 + test:1792-1800，no-write ordering 已测）
- [ ] 5.4 Record readonly DB validation evidence with redacted DSN, DB role type, commands, pass/fail/blocker status, and secret redaction. （C-live：脱敏/记录机制已实现 readonly_db_validation.py:3318-3346 + test:134-162，证据文件需真实运行产出）
- [x] 5.5 Add a focused validation entrypoint that reports `BLOCKED` or pytest skip when a real readonly DB URL is absent; it must not claim PASS from mock-only or writer credentials. （test:37-52,165-190 + CLI main BLOCKED + READONLY_DB_VALIDATION_SIMULATED 防冒充）
- [x] 5.6 Add tests for evidence redaction, approved evidence roots, rollback/idempotency of permission probes, writer-credential FAIL detection, and display retry/cancel no-write ordering. （test_readonly_db_validation.py 全覆盖）
- [x] 5.7 Verify normal local test runs remain independent of external PostgreSQL unless the readonly validation env is explicitly enabled. （test:1-50 delenv + _FakeReadonlyAdapter:1803-1947）
- [ ] 5.8 Cover at minimum `hydro.hydro_run`, `hydro.river_timeseries`, `met.forecast_cycle`, `met.forcing_station_timeseries` or station-equivalent, `ops.pipeline_job`, `ops.pipeline_event`,
  reachable-role inventory, and schema/table DDL evidence for `hydro`, `met`, and `ops`; absent reduced-fixture tables must be reported as `BLOCKED` or explicitly out of scope. （C-live：目标表/schema 覆盖逻辑已实现 readonly_db_validation.py:192-199 + test:1143-1162，DDL evidence 需真实只读 DB 产出）

## 6. Display Readonly Ops UI

- [x] 6.1 Use backend runtime config as the stable frontend role source for display mode; production `/ops` must not rely on hardcoded build-time role assumptions. （stores/monitoring.ts:195-219 + pages/MonitoringPage.tsx:81-82）
- [x] 6.2 Update `/ops` and monitoring job controls so `display_readonly` hides or disables real retry/cancel controls for all roles and never sends retry/cancel or Slurm control requests. （components/monitoring/JobsTable.tsx:59-346 + MonitoringPage.tsx:82,308,315）
- [x] 6.3 Make queue-depth UI optional in display mode: hide it or render a read-only unavailable state when the backend reports display-safe queue depth unavailable. （stores/monitoring.ts:287-300 + SummaryBar.tsx:89-98）
- [x] 6.4 Add strict identity context to `/ops` so stages, jobs, diagnostics, and log requests use or validate `source`, `cycle_time`, `run_id`, and `model_id`. （MonitoringPage.tsx:45-96,294-298 + stores/monitoring.ts:191-237）
- [x] 6.5 Add diagnostic copy behavior for failed jobs/stages with available `source_id`, `cycle_time`, `run_id`, `model_id`, `stage`, `job_id`, `slurm_job_id`, `status`, `error_code`, `error_message`, and `log_uri`. （components/monitoring/diagnostics.ts:13-82 + JobsTable.tsx:147-156）
- [x] 6.6 Add manual 22 recovery guidance and local-only notified/acknowledged UI state without DB writes. （JobsTable.tsx:110-164 + test:413-500）
- [x] 6.7 Add `/hydro-met` strict latest-product bootstrap from complete URL `source`, `cycle_time`, `run_id`, and `model_id`; partial strict identity is invalid and must not issue source-only fallback,
  while non-strict source-only browsing remains compatible. Direct local E2E handoff-file parsing is #239 harness scope. （pages/hydroMet/bootstrap.ts:45-163）
- [ ] 6.8 Add frontend tests for runtime config role source, display-mode hidden controls, no control POSTs, optional queue-depth unavailable state, strict ops identity, strict hydro-met handoff/non-strict compatibility,
  diagnostic payload content, published-log success/error display, local-only notified state, compute/dev controls still available, and role/query-state regressions. （C-live：单元测试已全覆盖（monitoring.test.ts/AppRoutes.test.tsx/JobsTable.test.tsx/bootstrap.test.ts），缺 display_readonly 模式的 e2e 浏览器场景 e2e/monitoring.spec.ts）

## 7. Docker Env and Compose Skeleton

- [x] 7.1 Add `infra/env/compute.example`, `infra/env/display.example`, and shared env documentation with canonical `NHMS_PUBLISHED_ARTIFACT_*` names plus role-specific required and forbidden variables.
- [x] 7.2 Add Docker preflight commands/scripts that record `docker version`, `docker compose version`, DockerRootDir, `docker system df`, `df -h`, configured `TMPDIR`, and evidence root before builds; low space is `BLOCKED`.
- [x] 7.3 Add `infra/compose.compute.yml` with compute-control services, writable workspace/published mounts, `scheduler-once` using an existing tested command, and no public control exposure by default.
- [x] 7.4 Add `infra/compose.display.yml` with display API, optional reverse proxy, readonly published artifact mount, readonly DB env, display role env, and no forbidden Slurm/Munge/workspace/Docker-socket mounts.
- [x] 7.5 Add compose/env static tests or scripts that detect forbidden display mounts/env, HostConfig hazards (`privileged`, host PID/IPC/network, broad host-root bind, Docker socket, `cap_add`), publish-root env drift, and accidental use of `infra/docker-compose.dev.yml` as a two-node production file.

Required evidence for #236:

- [x] 7.6 Static compose/env test command validates safe compute/display skeletons, unsafe display env/mount/HostConfig cases, publish-root drift, and dev-compose misuse.
- [x] 7.7 Docker preflight is run locally when Docker is available and writes evidence under `artifacts/` or `/scratch/frd_muziyao`; Docker unavailable or low space is reported as `BLOCKED`, not PASS.
- [x] 7.8 `docker compose -f infra/compose.compute.yml config` and `docker compose -f infra/compose.display.yml config` pass, or produce documented BLOCKED evidence if #237 image assets are required.
- [x] 7.9 `uv run ruff check <new Python script/test paths if Python>` and `openspec validate m22-two-node-docker-readonly-display --strict --no-interactive` pass.

## 8. Docker Image and Entrypoint

- [x] 8.1 Add `infra/docker/Dockerfile.app` for one default app image with backend dependencies and frontend static assets as needed for MVP.
- [x] 8.2 Add `infra/docker/entrypoint.sh` that validates `NHMS_REQUIRE_SERVICE_ROLE`, `NHMS_SERVICE_ROLE`, starts role-specific commands, rejects display startup with compute-only env, and does not start full business API for reserved `slurm_gateway`.
- [x] 8.3 Ensure the default app image does not install Slurm client or Munge; any Slurm Gateway container image remains optional and 22-only.
- [x] 8.4 Add Docker build smoke tests and image/runtime checks, with logs and temporary files under `artifacts/` or `/scratch/frd_muziyao`.

## 9. Systemd and Two-Node Docker E2E Docs

- [x] 9.1 Add `infra/systemd/nhms-compute-compose.service` and `infra/systemd/nhms-display-compose.service` examples that run compose from the repository `infra` directory.
- [x] 9.2 Add or document a 22 host Slurm Gateway systemd unit as the MVP-recommended first phase when gateway containerization is not yet proven, without claiming the APIRouter module is directly runnable unless a dedicated app exists.
- [x] 9.3 Add `infra/README.two-node-docker.md` covering topology, deployment order, canonical env names, Docker disk preflight, env files, compose commands, systemd install, security probes, evidence paths, rollback, and the dev-compose non-goal.
- [x] 9.4 Update the two-node E2E runbook or link it from Docker docs so Docker validation records compute, display, cross-plane, manual ops boundary, DB, API, browser, Slurm, logs, and Docker security evidence separately.

## 10. Docker E2E Verification

- [x] 10.1 Add Docker display security checks proving no Slurm CLI/config/socket, no Docker socket, no forbidden mounts/env or HostConfig hazards, `/api/v1/slurm/*` unavailable, published artifacts are readonly, and runtime config reports `display_readonly`.
- [x] 10.2 Add cross-plane E2E checks that require strict `run_id/source/cycle_time/model_id` latest-product matching before `/hydro-met` and `/ops` browser evidence can pass.
- [x] 10.3 Add GFS/IFS source-scope reporting: if the run includes both sources, both must pass strict latest/series/ops/logs/browser checks for cross-plane `PASS`; single-source or missing-source runs are reduced scope or `PARTIAL`.
- [x] 10.4 Add manual ops boundary checks proving 27 only displays retry/cancel outcomes produced by 22 and never creates control-plane receipts itself.
- [x] 10.5 Record focused verification commands for backend tests, frontend tests/build, Docker preflight/compose config/build smoke, readonly DB smoke, OpenSpec validation, and two-node E2E evidence paths.

---

## 对账尾注（2026-06-06）

- **B-尾巴（已补齐，2026-06-06）**：2.7、2.8、3.6 —— openapi 补 ControlPlaneManualActionRequired/ControlPlaneQueueUnavailable/JobLogError 契约 + drift/RBAC 矩阵测试，本地 ruff 绿 + 183 passed，前端类型 regen（纯增量）。
- **C-live（代码+单测已实现，PASS 证据需 node-27 实机 / 真实只读 DB / 浏览器）**：4.3、5.1、5.2、5.4、5.8、6.8，以及 §10 已勾的 cross-plane E2E 的真实 live run。
- 这些 C-live 项即 `progress.md`「仍需 live proof」节在 27 节点的落点；执行清单见 `docs/runbooks/node-27-bringup-checklist.md`。
