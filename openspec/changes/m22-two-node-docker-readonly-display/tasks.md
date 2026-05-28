## 0. Change Preflight and Baseline

- [ ] 0.1 Validate this OpenSpec change with `openspec validate m22-two-node-docker-readonly-display --strict --no-interactive` before implementation issues begin.
- [ ] 0.2 Record the current code baseline in the Epic: `apps/api/main.py` unconditionally mounts `slurm_router`, retry/cancel call gateway paths, `/jobs/{job_id}/logs` is local-path oriented, latest-product accepts only `source`, and production Docker assets are limited to `infra/docker-compose.dev.yml`.
- [ ] 0.3 Record the rollout invariant in the Epic: safety boundaries land before Docker deployment assets; Docker must not copy control-plane capability to 27.
- [ ] 0.4 Record the temporary/evidence path constraint in the Epic and Docker issues: project-created temp, review, Docker smoke, and E2E artifacts go under `artifacts/` or `/scratch/frd_muziyao`.

## 1. Runtime Service Role Boundary

- [ ] 1.1 Add a runtime role helper/config for `dev_monolith`, `compute_control`, `display_readonly`, and reserved-or-dedicated `slurm_gateway`.
- [ ] 1.2 Define the production-like predicate: missing role fails when `NHMS_REQUIRE_SERVICE_ROLE=true` or `NHMS_AUTH_MODE=production|live|live_idp`; local/test may default to `dev_monolith` only when those signals are absent.
- [ ] 1.3 Gate `apps/api/main.py` Slurm router registration by role so `display_readonly` does not register `/api/v1/slurm/*` and display OpenAPI does not advertise those routes.
- [ ] 1.4 Add display-role unsafe config detection for Slurm gateway env, compute workspace/Basins/SHUD env, and control mutations.
- [ ] 1.5 Add `GET /api/v1/runtime/config` or equivalent read-only runtime config endpoint returning service role and capability flags for frontend gating; update OpenAPI and generated frontend types.
- [ ] 1.6 Define `slurm_gateway` startup semantics: either reserved/fail-fast unless a dedicated gateway app exists, or a dedicated app that exposes only health and `/api/v1/slurm/*`.
- [ ] 1.7 Add focused tests for role parsing, production missing-role failure, display route absence, compute/dev route presence, display unsafe config blockers, runtime config output, and `slurm_gateway` route inventory or reserved-role failure.

## 2. Display Retry/Cancel Fail-Closed Backend

- [ ] 2.1 Add a display-mode guard to `POST /api/v1/runs/{run_id}/retry` that returns HTTP `409` with standard error envelope code `CONTROL_PLANE_MANUAL_ACTION_REQUIRED` and safe manual 22 recovery details for otherwise-authorized callers.
- [ ] 2.2 Add a display-mode guard to `POST /api/v1/runs/{run_id}/cancel` that returns HTTP `409` with standard error envelope code `CONTROL_PLANE_MANUAL_ACTION_REQUIRED` and safe manual 22 recovery details for otherwise-authorized callers.
- [ ] 2.3 Preserve existing auth/RBAC ordering so unauthenticated or unauthorized display retry/cancel callers receive existing `401` or `403` responses without manual recovery details.
- [ ] 2.4 Ensure display retry/cancel does not construct or call `get_slurm_gateway()`, does not submit/cancel jobs, and does not write pipeline/hydro/met terminal state.
- [ ] 2.5 Add display-mode `/api/v1/queue/depth` behavior that is DB-derived or returns a stable read-only unavailable error without constructing or calling `get_slurm_gateway()`.
- [ ] 2.6 Preserve compute-control/dev retry/cancel and queue-depth behavior plus existing RBAC semantics.
- [ ] 2.7 Update static/runtime OpenAPI, generated frontend types, API contract tests, and OpenAPI drift tests for the manual-action and queue-depth display contracts.
- [ ] 2.8 Add backend tests with gateway spies and DB state assertions for display retry/cancel and queue-depth no-side-effect behavior, auth/RBAC matrix, plus compute-control regression.

## 3. Published Artifact Log Reader

- [ ] 3.1 Add `services/artifacts` config, URI parsing, redaction, and reader support for `published://`, allowed publish-root `file://`, and allowlisted `s3://` log URIs using canonical env names `NHMS_PUBLISHED_ARTIFACT_ROOT`, `NHMS_PUBLISHED_ARTIFACT_URI_PREFIX`, `NHMS_PUBLISHED_ARTIFACT_S3_BUCKET`, and `NHMS_PUBLISHED_ARTIFACT_S3_PREFIX`.
- [ ] 3.2 Enforce publish-root containment, S3 bucket/prefix allowlist, symlink rejection, path traversal and encoded separator rejection, credential-bearing URI rejection, and `NHMS_LOG_TAIL_MAX_BYTES` tail limits.
- [ ] 3.3 Add compute-side log publication or URI normalization so newly recorded `ops.pipeline_job.log_uri` values use supported published artifact URIs, with canonical `published://logs/<source>/<cycle_time>/<run_id>/<job_id>.out|err` where practical.
- [ ] 3.4 Migrate `/api/v1/jobs/{job_id}/logs` to use ArtifactReader and return exact stable errors: `JOB_LOG_NOT_PUBLISHED`, `JOB_LOG_URI_UNSUPPORTED`, `JOB_LOG_ACCESS_DENIED`, and `JOB_LOG_NOT_FOUND`.
- [ ] 3.5 Preserve or explicitly gate legacy local `LOG_ROOT` behavior for `dev_monolith`; ensure `display_readonly` with `NHMS_DISPLAY_ALLOW_LOCAL_FILE_LOGS=false` cannot use private local paths.
- [ ] 3.6 Update static/runtime OpenAPI, generated frontend types, API contract tests, and OpenAPI drift tests for log errors and response metadata.
- [ ] 3.7 Add tests for compute log URI emission, published local logs, allowed `file://` logs, mocked allowlisted S3 logs, disallowed S3 bucket/prefix, tail limits, unsafe paths, private workspace paths, credential redaction, unsupported scheme, missing object, and API error mapping.

## 4. QHH Latest Product Strict Identity

- [ ] 4.1 Extend `GET /api/v1/mvp/qhh/latest-product` query parameters and OpenAPI schema to accept optional `run_id`, `cycle_time`, and `model_id` alongside `source`; if any strict identity field is present, all four strict fields are required for cross-plane proof.
- [ ] 4.2 Extend `packages/common/forecast_store.py` latest-product selection so strict filters must match exactly and never fall back to historical latest data.
- [ ] 4.3 Ensure ready and unavailable responses include safe identity details for `run_id`, `source_id`, `cycle_time`, `model_id`, basin identity, forcing version, basin version, and river network version where applicable.
- [ ] 4.4 Reject partial strict identity at the backend with `422 VALIDATION_ERROR`, safe missing/provided/required field details, and no source-only store lookup.
- [ ] 4.5 Update generated frontend API types plus API contract and OpenAPI drift tests for the backend query contract.
- [ ] 4.6 Add backend tests for source-only compatibility, strict match success, strict mismatch unavailable, partial strict identity rejection, duplicate same-source/cycle run/model mismatch, model/source/cycle normalization, no historical fallback, and strict SQL predicates.

Issue ownership note:

- #232 owns the backend latest-product API/store contract, OpenAPI schema, generated frontend type surface, and backend tests for source-only compatibility, strict success/mismatch, partial strict identity rejection, normalization, and no historical fallback.
- #233 owns `/ops` pipeline status/stages/jobs strict identity filtering.
- #235 owns `/hydro-met` frontend bootstrap, frontend strict query behavior, and browser/UI tests.
- #239 owns final cross-plane E2E evidence and pass/fail/partial reporting.

## 4A. Pipeline Ops Strict Run Identity

- [ ] 4A.1 Extend pipeline status, stages, jobs, and logs APIs or response metadata so strict consumers can bind `source`, `cycle_time`, `run_id`, and `model_id` together.
- [ ] 4A.2 Preserve existing source/cycle status, stages, jobs, pagination, sorting, status/stage filters, and log behavior for non-strict browsing.
- [ ] 4A.3 Reject partial strict ops identity with `422 VALIDATION_ERROR`, safe `missing_fields`, `provided_fields`, `required_fields`, and `strict_identity_required=true` before source/cycle-only lookup.
- [ ] 4A.4 Ensure strict status/stages/jobs evidence resolves to a concrete `hydro.hydro_run` identity and cannot use jobs from a same-source/cycle sibling run or model.
- [ ] 4A.5 Ensure strict job log evidence validates the requested `run_id` and `model_id` before reading the published log, and wrong-run jobs/logs return HTTP `409` with code `PIPELINE_STRICT_IDENTITY_MISMATCH`.
- [ ] 4A.6 Add duplicate same-source/cycle fixtures where two runs differ by `run_id` or `model_id`, including jobs/logs that would otherwise be mixed by source/cycle-only queries.
- [ ] 4A.7 Add strict identity error contract tests for `422 VALIDATION_ERROR`, `404 PIPELINE_STRICT_IDENTITY_NOT_FOUND`, and `409 PIPELINE_STRICT_IDENTITY_MISMATCH`, including safe details and log mismatch before artifact read.
- [ ] 4A.8 Update static/runtime OpenAPI, generated frontend API types, API contract tests, and OpenAPI drift tests for the strict ops identity contract.

## 5. Readonly DB Boundary

- [ ] 5.1 Add readonly DB validation tests or runbook commands proving display APIs work with readonly credentials for health, models, stations, latest-product, pipeline status, stages, jobs, logs, and runtime config.
- [ ] 5.2 Add readonly DB permission-denied probes that record `current_user` and prove controlled `INSERT`, `UPDATE`, `DELETE`, and DDL attempts are denied or rolled back for hydro, met, ops, and pipeline-critical tables.
- [ ] 5.3 Ensure display retry/cancel returns manual action before any write attempt when using readonly DB credentials.
- [ ] 5.4 Record readonly DB validation evidence with redacted DSN, DB role type, commands, pass/fail/blocker status, and secret redaction.

## 6. Display Readonly Ops UI

- [ ] 6.1 Use backend runtime config as the stable frontend role source for display mode; production `/ops` must not rely on hardcoded build-time role assumptions.
- [ ] 6.2 Update `/ops` and monitoring job controls so `display_readonly` hides or disables real retry/cancel controls for all roles and never sends retry/cancel or Slurm control requests.
- [ ] 6.3 Make queue-depth UI optional in display mode: hide it or render a read-only unavailable state when the backend reports display-safe queue depth unavailable.
- [ ] 6.4 Add strict identity context to `/ops` so stages, jobs, diagnostics, and log requests use or validate `source`, `cycle_time`, `run_id`, and `model_id`.
- [ ] 6.5 Add diagnostic copy behavior for failed jobs/stages with available `source_id`, `cycle_time`, `run_id`, `model_id`, `stage`, `job_id`, `slurm_job_id`, `status`, `error_code`, `error_message`, and `log_uri`.
- [ ] 6.6 Add manual 22 recovery guidance and local-only notified/acknowledged UI state without DB writes.
- [ ] 6.7 Add frontend tests for runtime config role source, display-mode hidden controls, no control POSTs, optional queue-depth unavailable state, strict ops identity, diagnostic payload content, published-log error display, compute/dev controls still available, and role/query-state regressions.

## 7. Docker Env and Compose Skeleton

- [ ] 7.1 Add `infra/env/compute.example`, `infra/env/display.example`, and shared env documentation with canonical `NHMS_PUBLISHED_ARTIFACT_*` names plus role-specific required and forbidden variables.
- [ ] 7.2 Add Docker preflight commands/scripts that record `docker version`, `docker compose version`, DockerRootDir, `docker system df`, `df -h`, configured `TMPDIR`, and evidence root before builds; low space is `BLOCKED`.
- [ ] 7.3 Add `infra/compose.compute.yml` with compute-control services, writable workspace/published mounts, `scheduler-once` using an existing tested command, and no public control exposure by default.
- [ ] 7.4 Add `infra/compose.display.yml` with display API, optional reverse proxy, readonly published artifact mount, readonly DB env, display role env, and no forbidden Slurm/Munge/workspace/Docker-socket mounts.
- [ ] 7.5 Add compose/env static tests or scripts that detect forbidden display mounts/env, HostConfig hazards (`privileged`, host PID/IPC/network, broad host-root bind, Docker socket, `cap_add`), publish-root env drift, and accidental use of `infra/docker-compose.dev.yml` as a two-node production file.

## 8. Docker Image and Entrypoint

- [ ] 8.1 Add `infra/docker/Dockerfile.app` for one default app image with backend dependencies and frontend static assets as needed for MVP.
- [ ] 8.2 Add `infra/docker/entrypoint.sh` that validates `NHMS_REQUIRE_SERVICE_ROLE`, `NHMS_SERVICE_ROLE`, starts role-specific commands, rejects display startup with compute-only env, and does not start full business API for reserved `slurm_gateway`.
- [ ] 8.3 Ensure the default app image does not install Slurm client or Munge; any Slurm Gateway container image remains optional and 22-only.
- [ ] 8.4 Add Docker build smoke tests and image/runtime checks, with logs and temporary files under `artifacts/` or `/scratch/frd_muziyao`.

## 9. Systemd and Two-Node Docker E2E Docs

- [ ] 9.1 Add `infra/systemd/nhms-compute-compose.service` and `infra/systemd/nhms-display-compose.service` examples that run compose from the repository `infra` directory.
- [ ] 9.2 Add or document a 22 host Slurm Gateway systemd unit as the MVP-recommended first phase when gateway containerization is not yet proven, without claiming the APIRouter module is directly runnable unless a dedicated app exists.
- [ ] 9.3 Add `infra/README.two-node-docker.md` covering topology, deployment order, canonical env names, Docker disk preflight, env files, compose commands, systemd install, security probes, evidence paths, rollback, and the dev-compose non-goal.
- [ ] 9.4 Update the two-node E2E runbook or link it from Docker docs so Docker validation records compute, display, cross-plane, manual ops boundary, DB, API, browser, Slurm, logs, and Docker security evidence separately.

## 10. Docker E2E Verification

- [ ] 10.1 Add Docker display security checks proving no Slurm CLI/config/socket, no Docker socket, no forbidden mounts/env or HostConfig hazards, `/api/v1/slurm/*` unavailable, published artifacts are readonly, and runtime config reports `display_readonly`.
- [ ] 10.2 Add cross-plane E2E checks that require strict `run_id/source/cycle_time/model_id` latest-product matching before `/hydro-met` and `/ops` browser evidence can pass.
- [ ] 10.3 Add GFS/IFS source-scope reporting: if the run includes both sources, both must pass strict latest/series/ops/logs/browser checks for cross-plane `PASS`; single-source or missing-source runs are reduced scope or `PARTIAL`.
- [ ] 10.4 Add manual ops boundary checks proving 27 only displays retry/cancel outcomes produced by 22 and never creates control-plane receipts itself.
- [ ] 10.5 Record focused verification commands for backend tests, frontend tests/build, Docker preflight/compose config/build smoke, readonly DB smoke, OpenSpec validation, and two-node E2E evidence paths.
