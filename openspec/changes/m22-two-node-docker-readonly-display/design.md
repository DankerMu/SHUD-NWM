## Context

The repository has converged on QHH/limited-basin MVP display and operations, but the implementation still has monolith assumptions that are unsafe for a two-node deployment. `apps/api/main.py` imports and mounts `slurm_router` unconditionally. `POST /api/v1/runs/{run_id}/retry` and `POST /api/v1/runs/{run_id}/cancel` call the local Slurm gateway path and can mutate pipeline state. `/api/v1/jobs/{job_id}/logs` resolves `log_uri` through local filesystem logic rooted at `LOG_ROOT`. `GET /api/v1/mvp/qhh/latest-product` currently accepts only `source`, which is acceptable for product browsing but not enough to prove that 27 consumed the exact run produced by 22.

The target topology is:

- 22 node: `compute_control`, scheduler/plan-production, Slurm/Gateway, production writes, artifact publisher, recovery runbook.
- 27 node: `display_readonly`, FastAPI read APIs, frontend, `/ops`, readonly DB credentials, readonly published artifacts.
- Shared layer: PostgreSQL plus published artifacts. These are the only cross-node data surfaces for MVP.

Docker is desirable for repeatable deployment, but Docker must not copy the single-node control surface to 27. This change therefore lands safety boundaries first, then adds Docker assets and tests.

## Goals / Non-Goals

**Goals:**

- Make role identity explicit and fail fast in production when missing or unsafe.
- Ensure 27 cannot expose Slurm routes or execute retry/cancel even if mock gateway settings are present.
- Ensure 27 logs come from published artifacts, not 22 private workspace or `.nhms-runs`.
- Let cross-plane E2E query latest-product with the exact `run_id/source/cycle_time/model_id` identity from 22 evidence.
- Make `/ops` read-only in display mode while still useful for diagnostics and manual recovery.
- Add Docker runtime assets that encode physical capability separation: 22 gets compute mounts and writable publish root; 27 gets readonly DB and readonly published artifacts only.
- Add Docker and readonly DB tests now that local Docker permission is available.
- Keep temporary test/build/evidence outputs under this repository or `/scratch/frd_muziyao`, not the system disk.

**Non-Goals:**

- Introduce automatic operation requests or a 27-to-22 control API.
- Require Slurm Gateway containerization before the host-service path is proven.
- Add a scheduler daemon unless a real, tested loop entrypoint already exists; first Docker lane uses `scheduler-once`, systemd timer, or cron.
- Redesign QHH production algorithms, forcing, SHUD runtime, parser, or frontend visualization scope beyond read-only identity and diagnostics.

## Decisions

### Decision 1: Role Contract Before Docker

Add a small runtime role module such as `apps/api/runtime_mode.py` with an enum:

```text
dev_monolith
compute_control
display_readonly
slurm_gateway
```

`dev_monolith` remains the local/test default. In production-like modes, the app must require `NHMS_SERVICE_ROLE`; missing or unknown values fail fast. `display_readonly` must be able to run with readonly DB credentials and without Slurm settings.

The production-like predicate is explicit: missing role fails when `NHMS_REQUIRE_SERVICE_ROLE=true` or when existing production auth modes such as `NHMS_AUTH_MODE=production|live|live_idp` are active. Local/test defaulting to `dev_monolith` is allowed only when the require flag and production auth mode are absent. Docker and systemd examples must always set `NHMS_REQUIRE_SERVICE_ROLE=true` and an explicit role.

Alternative considered: use separate images or branches for 22 and 27. That increases drift risk in OpenAPI/types/dependencies and makes bug reproduction harder. A single image with role-gated entrypoints keeps code consistent while deployment removes physical capabilities from 27.

### Decision 2: Route Exposure Is Gated at Startup

`apps/api/main.py` must include the Slurm router only when the current role allows it. `display_readonly` must not mount `/api/v1/slurm/*`, and OpenAPI for a display-mode app must not advertise Slurm operations. `compute_control`, `slurm_gateway`, and `dev_monolith` may expose the router as appropriate for their role and tests.

Alternative considered: leave routes mounted but reject at handler time. That still advertises control-plane surface on 27 and leaves room for handler-specific bypasses. Startup gating is clearer and easier to test.

### Decision 3: Retry/Cancel Fail Closed in Display Mode

In `display_readonly`, retry/cancel returns a stable typed error:

```text
CONTROL_PLANE_MANUAL_ACTION_REQUIRED
```

The manual-action response uses HTTP `409 Conflict` with the standard API error envelope. Details include safe context such as `run_id`, `display_mode`, `suggested_action`, and `recovery_runbook`. Existing authentication and RBAC still run first: unauthenticated callers receive the existing `401`, unauthorized callers receive the existing `403`, and only otherwise-authorized callers receive the display-mode `409`. The display guard must not call `get_slurm_gateway()`, submit/cancel jobs, insert events, update job/run state, or mutate hydro/met/pipeline terminal state.

Alternative considered: hide UI only. Direct API calls and old clients would still reach the backend, so backend fail-closed behavior is required.

### Decision 4: Adjacent Queue State Does Not Reintroduce Gateway Access

`GET /api/v1/queue/depth` currently derives queue state through the Slurm gateway path. In `display_readonly`, that endpoint must either return a DB-derived read-only queue summary or a stable read-only unavailable response such as `CONTROL_PLANE_QUEUE_UNAVAILABLE`; it must not construct `get_slurm_gateway()` or use a mock gateway. `/ops` must treat queue depth as optional in display mode and degrade without failing the whole page.

Alternative considered: leave queue depth unchanged because it is a read endpoint. That still introduces Slurm/Gateway dependencies on 27 and can use mock data, so it violates the display boundary.

### Decision 5: ArtifactReader Owns Published Log Access and Producers Normalize Log URIs

Introduce `services/artifacts` with URI parsing, config, and reader code. The log route delegates to `ArtifactReader.read_text_tail(log_uri)` and returns a redacted safe `log_uri`.

MVP-supported URI forms:

- `published://logs/...`
- `file://<allowed-publish-root>/logs/...`
- `s3://<allowed-bucket>/<allowed-prefix>/logs/...`

The canonical runtime env names are:

```text
NHMS_PUBLISHED_ARTIFACT_ROOT
NHMS_PUBLISHED_ARTIFACT_URI_PREFIX=published://
NHMS_PUBLISHED_ARTIFACT_S3_BUCKET
NHMS_PUBLISHED_ARTIFACT_S3_PREFIX
NHMS_PUBLISHED_ARTIFACT_HOST_ROOT  # compose-only host mount source when different from container root
```

`PUBLISHED_ARTIFACT_ROOT` without the `NHMS_` prefix is not a runtime app env. Compose may use `NHMS_PUBLISHED_ARTIFACT_HOST_ROOT` as the host path and mount it to the in-container `NHMS_PUBLISHED_ARTIFACT_ROOT`.

This change also covers the write side: 22 production paths must write `ops.pipeline_job.log_uri` as a supported published artifact URI. The canonical MVP form is `published://logs/<source>/<cycle_time>/<run_id>/<job_id>.out|err`. Existing object-store `s3://.../runs/<run_id>/logs/...` values may remain compatible only when the bucket/prefix is explicitly allowlisted as a published log namespace.

Rejected forms include private workspace paths, `.nhms-runs`, 22 private `/scratch`, `/tmp`, path traversal, backslashes, encoded separators, userinfo, query, fragments, and any URI carrying apparent credentials. `NHMS_DISPLAY_ALLOW_LOCAL_FILE_LOGS=false` means display mode must not fall back to legacy local `LOG_ROOT` behavior.

Alternative considered: mount 22 workspace on 27. That violates the topology and can leak private paths, transient files, or credentials.

### Decision 6: Latest Product Has Two Modes and Strict Handoff Inputs

The source-only latest-product query remains useful for interactive browsing. Cross-plane E2E must also support strict filters:

```text
source
run_id
cycle_time
model_id
```

When strict filters are present, the store must return only a matching ready product or a typed unavailable response with reasons. It must not silently fall back to historical latest data.

`/hydro-met` strict handoff uses URL query parameters `source`, `cycle_time`, `run_id`, and `model_id`. If any strict identity parameter is present, all four are required. Browser E2E may also read the same four-field identity from `artifacts/two-node-e2e/<run_id>/cross-plane/identity.json`, but the frontend request to latest-product must still contain all four filters before cross-plane PASS can be claimed.

Alternative considered: make E2E call multiple lower-level endpoints and compare manually. That duplicates identity logic outside the product contract and lets UI/bootstrap code drift.

### Decision 7: Runtime Config Endpoint Drives Frontend Role Behavior

Add a small read-only runtime config API, for example `GET /api/v1/runtime/config`, returning the current service role and capability flags such as `control_mutations_enabled`, `slurm_routes_enabled`, `queue_depth_mode`, and `display_readonly`. The frontend uses this endpoint as the source of truth for display behavior. Build-time env may provide defaults for development, but production display UI behavior must come from the backend service.

Alternative considered: use build-time frontend env only. That can drift from the backend role inside one image and is hard to prove in Docker E2E.

### Decision 8: Ops UI Is Role-Aware and Diagnostic-First

`/ops` can reuse monitoring components, but in `display_readonly` it hides real retry/cancel controls for all roles, including operator/sys_admin. It shows failed jobs, logs, error code/message, copied diagnostic payload, and 22 runbook guidance. The "notified" state is local UI state in MVP and does not write DB.

Ops E2E must bind jobs/logs to the same strict run identity as latest-product. Backend pipeline status, stages, and jobs APIs therefore need either `run_id` and `model_id` filters or response metadata that lets the UI reject mismatched runs. If two runs share a source/cycle, `/ops` must not mix them.

Alternative considered: leave operator controls visible but show backend error after click. That trains operators to expect 27 to control compute and makes the read-only boundary less obvious.

### Decision 9: Docker Assets Encode Physical Separation

Add one app image and role-specific compose files:

- 22 compute: writable `WORKSPACE_ROOT`, writable `NHMS_PUBLISHED_ARTIFACT_ROOT`, optional compute API, `scheduler-once` command, and optional host or container Slurm Gateway only on 22.
- 27 display: `NHMS_SERVICE_ROLE=display_readonly`, readonly published artifact mount, readonly DB URL, no `/etc/slurm`, no `/run/munge`, no workspace, no Basins root, no Docker socket, no `SLURM_GATEWAY_URL`.

`infra/docker-compose.dev.yml` remains a development file and must not be promoted into production compose.

Display containers must also pass HostConfig checks: no `privileged`, no host PID/IPC/network, no Docker socket, no broad host root bind, no `cap_add`, no Slurm/Munge/config mounts, and readonly root filesystem where feasible. The app image must not contain Slurm client or Munge by default.

### Decision 10: Docker Disk Preflight and Evidence Paths

Docker build cache cannot be completely controlled by repository code, so Docker smoke must record the Docker root and cache state before building. Project-created artifacts, codeagent reviews, compose output, test evidence, and optional Docker smoke workdirs must default to:

```text
artifacts/stage-change/m22-two-node-docker-readonly-display/
artifacts/two-node-e2e/<run_id>/
/scratch/frd_muziyao/<project-specific-dir>/
```

Docker preflight records `docker info` DockerRootDir, `docker system df`, relevant `df -h`, and the configured evidence/TMPDIR. Low space marks Docker smoke `BLOCKED` instead of continuing. Issue bodies must repeat this constraint for Docker and E2E work.

## Risks / Trade-offs

- **Risk: production deployments start without `NHMS_SERVICE_ROLE`.** Mitigation: fail fast outside local/dev contexts and document role env examples.
- **Risk: display mode still imports Slurm modules.** Mitigation: route exposure and handler tests assert no `/api/v1/slurm/*`; follow-up hardening can move imports behind role checks if import-time dependencies prove unsafe.
- **Risk: retry/cancel dependencies call gateway before the handler guard.** Mitigation: ensure FastAPI dependency order does not construct `get_slurm_gateway` in display mode; use dependency tests/spies.
- **Risk: artifact URI support expands the path attack surface.** Mitigation: centralized parser, deny-by-default schemes, publish-root relative checks, encoded separator rejection, symlink checks, tail limit, and redacted errors.
- **Risk: strict latest filters require query changes across API/types/UI.** Mitigation: keep source-only mode compatible and add strict filters additively.
- **Risk: Docker image with Slurm client accidentally lands on 27.** Mitigation: default app image excludes Slurm/Munge; display compose tests assert no Slurm CLI/config/socket and no forbidden env/mounts.
- **Risk: readonly DB causes latent write paths to fail.** Mitigation: targeted readonly DB smoke for display routes plus retry/cancel fail-closed tests before Docker deployment.

## Issue #229 Fixture: Runtime Service Role Boundary

Fixture level: expanded
Repair intensity: high
Project profile: other

Change surface:

- `apps/api/main.py` FastAPI app construction, router registration, OpenAPI generation, and runtime config route.
- New or updated backend runtime role/config helper module.
- Static OpenAPI/generated frontend type surfaces touched by the runtime config contract.
- Tests covering role parsing, startup failure, route inventory, runtime config, unsafe display config, and reserved `slurm_gateway`.

Must preserve:

- Local/test startup without explicit role defaults to `dev_monolith` when production-like signals are absent.
- Existing compute/dev route inventory keeps Slurm routes available.
- Existing auth/RBAC behavior remains the source of authorization decisions; this issue does not change retry/cancel handler semantics.
- Business API routes other than `/api/v1/slurm/*` remain available for `dev_monolith`, `compute_control`, and `display_readonly`.

Must add/change:

- `NHMS_SERVICE_ROLE` supports `dev_monolith`, `compute_control`, `display_readonly`, and reserved `slurm_gateway`.
- Missing or unknown role fails fast when `NHMS_REQUIRE_SERVICE_ROLE=true` or `NHMS_AUTH_MODE` is production-like (`production`, `live`, `live_idp`).
- `display_readonly` does not register `/api/v1/slurm/*`, and display-mode OpenAPI does not advertise Slurm operations.
- `display_readonly` reports unsafe Slurm gateway or compute-path configuration as startup/preflight blockers before serving.
- `GET /api/v1/runtime/config` reports service role and capability flags for frontend gating.
- `slurm_gateway` does not start the full business API unless a dedicated bounded gateway app exists in this issue; MVP choice is reserved/fail-fast.

Selected risk packs:

- Public API / CLI / script entry: selected - adds runtime config API and changes route inventory by role.
- Config / project setup: selected - introduces role env and production-like missing-role behavior.
- File IO / path safety / overwrite: not selected - no new file reads/writes or path traversal surface in this issue.
- Schema / columns / units / field names: selected - runtime config/OpenAPI/frontend type contract changes.
- Geospatial / CRS / shapefile sidecars: not selected - no geospatial data handling.
- Time series / forcing / temporal boundaries: not selected - no forcing/time-series selection changes.
- Numerical stability / conservation / NaN: not selected - no solver or numerical behavior.
- Solver runtime / performance / threading: not selected - no solver runtime or threading behavior.
- Resource limits / large input / discovery: not selected - no directory discovery, large reads, polling, or subprocess waits.
- Legacy compatibility / examples: selected - local/dev monolith and compute-control tests must keep existing behavior.
- Error handling / rollback / partial outputs: selected - startup/config errors must be stable and fail before serving unsafe routes.
- Release / packaging / dependency compatibility: selected - Docker/systemd follow-up depends on stable env names and route contract.
- Documentation / migration notes: not selected - docs/systemd are owned by later Docker/runbook issues.

Invariant Matrix:

- Governing invariant: A running API process exposes only the capabilities allowed by its explicit or safe local-default service role, and production-like startup never silently falls back to a broader role.
- Source-of-truth identity/contract: `NHMS_SERVICE_ROLE`, `NHMS_REQUIRE_SERVICE_ROLE`, and production-like `NHMS_AUTH_MODE`.
- Producers: environment parsing/runtime role helper.
- Validators/preflight: role validation, production-like missing-role check, display unsafe-config guard, reserved `slurm_gateway` guard.
- Storage/cache/query: none - this issue does not persist role state.
- Public routes/entrypoints: FastAPI app startup, router registration, OpenAPI schema, `GET /api/v1/runtime/config`.
- Frontend/downstream consumers: generated frontend API/types and future `/ops` runtime config consumer.
- Failure paths/rollback/stale state: startup failure or stable config blocker before unsafe routes are served.
- Evidence/audit/readiness: focused tests and OpenAPI drift/contract checks.
- Regression rows:
  - `display_readonly` with explicit safe env -> API starts, `/api/v1/runtime/config` reports display flags, `/api/v1/slurm/*` is absent from routes and OpenAPI.
  - production-like startup with missing or unknown role -> stable configuration error before serving requests.
  - `display_readonly` with Slurm gateway or compute-only env -> stable display boundary blocker before serving requests.
  - `dev_monolith` local default and `compute_control` explicit role -> existing business routes and Slurm route availability remain compatible.
  - `slurm_gateway` role without dedicated gateway app -> stable reserved-role startup failure and no business API surface.

Boundary-surface checklist:

- Shared helper roots: runtime role/config helper.
- Public entrypoints: FastAPI app module import/startup, route inventory, OpenAPI route.
- Read surfaces: environment variables used to derive role and production-like mode.
- Write/delete/overwrite surfaces: none.
- Staging/publish/rollback surfaces: none.
- Producer/consumer evidence boundaries: runtime config OpenAPI schema and generated frontend types.
- Stale-state/idempotency boundaries: repeated app construction under different env values in tests must not leak prior role state.
- Unchanged downstream consumers: existing API contract tests and local/dev tests.

Required evidence:

- `uv run pytest -q tests/test_runtime_mode.py tests/test_api_contract.py tests/test_openapi_drift.py`: role parsing/startup, route inventory, runtime config contract, OpenAPI drift.
- `uv run ruff check apps/api services/slurm_gateway tests/test_runtime_mode.py`: style/static verification for touched backend paths.
- Frontend type generation/check command used by the repo after generated type changes, or an explicit no-op rationale if the static OpenAPI/type generation contract is unchanged by implementation mechanics.

Non-goals:

- Do not implement retry/cancel fail-closed behavior; that is #230.
- Do not implement ArtifactReader, latest-product strict identity, Docker compose/image/systemd, frontend `/ops` behavior, or readonly DB validation in this issue.

## Issue #230 Fixture: Display Control Mutation Guard

Fixture level: expanded
Repair intensity: high
Project profile: other

Change surface:

- `apps/api/routes/pipeline.py` retry, cancel, queue-depth dependencies and handler behavior.
- Runtime role integration via `apps/api/runtime_mode.py` and `request.app.state.runtime_config`.
- Backend tests for auth/RBAC ordering, no gateway construction, no DB writes, queue-depth display behavior, and compute/dev regressions.
- Static OpenAPI/generated frontend type surfaces only if response/error contracts require schema or type changes.

Must preserve:

- Existing auth/RBAC ordering: unauthenticated and unauthorized callers still receive existing `401`/`403` responses before any display manual-action details.
- Existing compute-control and dev-monolith retry/cancel behavior, including gateway submission/cancellation, retry metadata, partial cancellation handling, and audit behavior.
- Existing compute-control and dev-monolith `/api/v1/queue/depth` gateway-backed behavior.
- Existing public success response shapes for compute/dev retry, cancel, and queue-depth.

Must add/change:

- In `display_readonly`, otherwise-authorized retry and cancel calls return HTTP `409` with standard error envelope code `CONTROL_PLANE_MANUAL_ACTION_REQUIRED`.
- Manual-action details include safe `run_id`, `display_mode=display_readonly`, `suggested_action`, and `recovery_runbook`; they do not claim a submitted/cancelled job.
- Display retry/cancel guards must not construct or call `get_slurm_gateway()`, must not call submit/cancel, and must not write pipeline events, pipeline jobs, hydro status, met/forecast-cycle status, or terminal state.
- In `display_readonly`, `/api/v1/queue/depth` returns stable read-only unavailable error `CONTROL_PLANE_QUEUE_UNAVAILABLE` unless a DB-derived summary is implemented; MVP choice is stable unavailable.
- Display queue-depth must not construct/call Slurm gateway, mock gateway, `queue_depth()`, or `list_jobs()`.

Selected risk packs:

- Public API / CLI / script entry: selected - changes public retry/cancel/queue-depth behavior by runtime role.
- Config / project setup: selected - behavior depends on `NHMS_SERVICE_ROLE=display_readonly`.
- File IO / path safety / overwrite: not selected - no new file reads/writes or path traversal surface.
- Schema / columns / units / field names: selected - stable API error codes/details and OpenAPI/types must stay aligned.
- Geospatial / CRS / shapefile sidecars: not selected - no geospatial data handling.
- Time series / forcing / temporal boundaries: not selected - no forcing/time-series selection changes.
- Numerical stability / conservation / NaN: not selected - no solver or numerical behavior.
- Solver runtime / performance / threading: not selected - no solver runtime or threading behavior.
- Resource limits / large input / discovery: not selected - no directory discovery, large reads, polling, or subprocess waits.
- Legacy compatibility / examples: selected - compute/dev retry/cancel/queue-depth behavior must remain compatible.
- Error handling / rollback / partial outputs: selected - display manual-action and queue-depth unavailable errors must be stable and no side effects.
- Release / packaging / dependency compatibility: selected - Docker/display deployment depends on fail-closed backend behavior.
- Documentation / migration notes: not selected - frontend/runbook text is owned by later issues.

Invariant Matrix:

- Governing invariant: A display-readonly API may reveal authorized manual recovery guidance but must never perform or prepare control-plane mutations, construct gateway dependencies, or write terminal state; compute/dev roles retain existing behavior.
- Source-of-truth identity/contract: `request.app.state.runtime_config.service_role` after auth/RBAC succeeds for protected mutations.
- Producers: runtime config from `apps/api/runtime_mode.py` and FastAPI app state.
- Validators/preflight: retry/cancel auth dependencies, safe run-id validation, display guard dependency before gateway/store mutation dependencies.
- Storage/cache/query: pipeline store, hydro/met/forecast tables, and pipeline event/job rows must remain unchanged for display retry/cancel.
- Public routes/entrypoints: `POST /api/v1/runs/{run_id}/retry`, `POST /api/v1/runs/{run_id}/cancel`, `GET /api/v1/queue/depth`.
- Frontend/downstream consumers: generated API types and future `/ops` UI that will consume manual-action/unavailable errors.
- Failure paths/rollback/stale state: display guards return stable errors without partial writes; unauthorized display requests return auth errors without manual recovery details.
- Evidence/audit/readiness: focused backend tests with gateway/store spies, state snapshots, API contract/OpenAPI drift tests, and compute/dev regression tests.
- Regression rows:
  - display authorized retry/cancel -> `409 CONTROL_PLANE_MANUAL_ACTION_REQUIRED`, safe details, no gateway construction/calls, no DB writes/events/state changes.
  - display unauthenticated or unauthorized retry/cancel -> existing `401`/`403`, no manual recovery details, no gateway/store mutation.
  - display queue-depth -> `503 CONTROL_PLANE_QUEUE_UNAVAILABLE`, no gateway construction/calls.
  - compute/dev retry/cancel/queue-depth -> existing successful and error behavior preserved.
  - invalid `run_id` -> existing validation behavior remains stable and does not leak manual details.

Boundary-surface checklist:

- Shared helper roots: pipeline route dependency helpers and runtime role helper.
- Public entrypoints: retry/cancel/queue-depth routes.
- Read surfaces: runtime config role, auth context, existing pipeline store queries needed by compute/dev only.
- Write/delete/overwrite surfaces: retry job/event writes, cancel job/event/hydro/forecast-cycle writes; display must not touch them.
- Staging/publish/rollback surfaces: none.
- Producer/consumer evidence boundaries: OpenAPI error responses, generated frontend types, future `/ops` consumer expectations.
- Stale-state/idempotency boundaries: display retry/cancel repeated calls must remain side-effect free; compute/dev idempotency remains unchanged.
- Unchanged downstream consumers: existing monitoring API and retry/cancel consistency tests.

Required evidence:

- `uv run pytest -q tests/test_runtime_mode.py tests/test_monitoring_api.py tests/test_retry_cancel_consistency.py tests/test_api_contract.py tests/test_openapi_drift.py`: display fail-closed, auth ordering, no gateway/no-write, compute/dev regressions, OpenAPI/contract drift.
- `uv run ruff check apps/api tests/test_runtime_mode.py tests/test_monitoring_api.py tests/test_retry_cancel_consistency.py`: style/static verification.
- Frontend API type check if OpenAPI/static schema changes are made, otherwise explicit no-op rationale.

Non-goals:

- Do not implement frontend `/ops` hiding/diagnostics; that is #235.
- Do not implement ArtifactReader or published log reading; that is #231.
- Do not implement Docker compose/image/systemd or readonly DB validation; those are later M22 issues.

## Issue #231 Fixture: Published Artifact Log Writer and Reader Contract

Fixture level: expanded
Repair intensity: broad-expanded
Project profile: other

Change surface:

- New `services/artifacts` reader/config/URI parsing/redaction boundary.
- `apps/api/routes/pipeline.py` job log route and API error mapping.
- Compute-side log URI emission/normalization in `services/orchestrator/chain.py` and any direct pipeline job persistence helpers it uses.
- Existing object-store/storage/safe-fs helpers only where needed for shared validation or no-follow bounded reads.
- OpenAPI/static generated frontend types only when response schema or documented error contracts require changes.
- Backend tests for published local files, allowlisted S3, unsafe paths, credential redaction, missing objects, exact error mapping, and compute-side `log_uri` emission.

Must preserve:

- Existing `GET /api/v1/jobs/{job_id}/logs` success envelope shape for safe dev/local logs unless schema changes are explicitly made and generated types are updated.
- Existing `404 JOB_NOT_FOUND` behavior when the pipeline job row does not exist.
- Existing local/dev-monolith log usability for safe local `LOG_ROOT` paths when legacy local access is explicitly allowed.
- Existing object-store key validation semantics for non-log artifacts unless this issue intentionally extends them for published log namespaces.
- Existing scheduler/orchestrator job state transitions and retry/cancel behavior aside from normalizing supported log URIs.

Must add/change:

- Add `services/artifacts` config using canonical runtime env names `NHMS_PUBLISHED_ARTIFACT_ROOT`, `NHMS_PUBLISHED_ARTIFACT_URI_PREFIX`, `NHMS_PUBLISHED_ARTIFACT_S3_BUCKET`, `NHMS_PUBLISHED_ARTIFACT_S3_PREFIX`, and compose-only `NHMS_PUBLISHED_ARTIFACT_HOST_ROOT`.
- Support bounded tail reads for `published://logs/...`, `file://` paths under the configured published artifact root, and `s3://` objects under the configured bucket/prefix allowlist.
- Reject private local paths, `.nhms-runs`, 22 private `/scratch`, `/tmp`, relative local paths outside the publish root, path traversal, encoded traversal/separators, backslashes, symlink escapes, userinfo, query strings, fragments, tokens, signatures, and credential-like URI parts.
- Migrate `/api/v1/jobs/{job_id}/logs` to `ArtifactReader` and map failures to exact stable errors: `JOB_LOG_NOT_PUBLISHED`, `JOB_LOG_URI_UNSUPPORTED`, `JOB_LOG_ACCESS_DENIED`, and `JOB_LOG_NOT_FOUND`.
- Ensure API responses use safe public/redacted URI summaries and do not leak private absolute paths, credentials, signed query strings, or raw backend exception text.
- Normalize newly recorded production pipeline job log URIs to a display-readable supported URI where practical, with canonical MVP form `published://logs/<source>/<cycle_time>/<run_id>/<job_id>.out|err`.
- Gate legacy local `LOG_ROOT` fallback so `display_readonly` with `NHMS_DISPLAY_ALLOW_LOCAL_FILE_LOGS=false` cannot read private local paths.

Selected risk packs:

- Public API / CLI / script entry: selected - changes public job log API failure contracts and safe URI metadata.
- Config / project setup: selected - introduces canonical published artifact env and display local-file gating.
- File IO / path safety / overwrite: selected - reader resolves local file URIs and must reject traversal, symlinks, private paths, and unsafe components.
- Schema / columns / units / field names: selected - stable API error codes/details, possible response metadata, OpenAPI/static type alignment, and `ops.pipeline_job.log_uri` values.
- Geospatial / CRS / shapefile sidecars: not selected - no geospatial file or CRS handling.
- Time series / forcing / temporal boundaries: not selected - no forcing/time-series selection changes.
- Numerical stability / conservation / NaN: not selected - no solver or numerical behavior.
- Solver runtime / performance / threading: not selected - no solver runtime or threading behavior.
- Resource limits / large input / discovery: selected - bounded log tail reads and no unbounded S3/local reads.
- Legacy compatibility / examples: selected - dev/local log behavior and existing job log route compatibility must remain safe.
- Error handling / rollback / partial outputs: selected - exact log error mapping must not depend on raw filesystem/S3 exceptions.
- Release / packaging / dependency compatibility: selected - Docker/display deployment depends on published artifact env names and readonly log access.
- Documentation / migration notes: not selected - Docker/runbook docs are owned by later issues except comments required to explain new env names in code/tests.

Invariant Matrix:

- Governing invariant: Display-readable job logs may be read only from an explicitly published artifact namespace, with bounded/redacted access, and newly produced production `log_uri` values must identify that same namespace without exposing private compute paths.
- Source-of-truth identity/contract: normalized `PipelineJob.log_uri` plus `ArtifactReaderConfig` derived from canonical `NHMS_PUBLISHED_ARTIFACT_*` env and display local-file gating.
- Producers: orchestrator/chain log URI emission and gateway log persistence; existing pipeline job persistence helpers that store `log_uri`.
- Validators/preflight: URI parser, published-root resolver, S3 bucket/prefix allowlist, local-file legacy gate, credential/query/fragment rejection, traversal/encoded-separator rejection, symlink/no-follow checks, tail limit validation.
- Storage/cache/query: `ops.pipeline_job.log_uri` rows, published artifact root files, allowlisted S3 objects, and legacy local `LOG_ROOT` only when explicitly allowed.
- Public routes/entrypoints: `GET /api/v1/jobs/{job_id}/logs` and any compute-side job submission/status paths that set log URI.
- Frontend/downstream consumers: generated API types and future `/ops` log diagnostics that distinguish not-published, unsupported, access-denied, and not-found failures.
- Failure paths/rollback/stale state: unsafe/missing/unsupported logs return stable redacted errors without opening private paths or attempting unallowlisted S3 reads; publication failure must not corrupt job state.
- Evidence/audit/readiness: artifact reader unit tests, API route tests, compute-side log URI emission tests, API contract/OpenAPI drift tests, and redaction assertions for every unsafe URI class.
- Regression rows:
  - `published://logs/<source>/<cycle_time>/<run_id>/<job_id>.out` under configured root -> bounded tail content, safe public `log_uri`, no private path leak.
  - allowed `file://<published-root>/logs/...` -> bounded tail content; symlink or resolved path outside root -> `JOB_LOG_ACCESS_DENIED`.
  - allowlisted `s3://<bucket>/<prefix>/logs/...` -> bounded tail content via mocked object reader; unallowlisted bucket/prefix -> stable forbidden/unsupported error and no read attempt.
  - no `log_uri` -> `JOB_LOG_NOT_PUBLISHED`; unsupported scheme/private path/malformed credential-bearing URI -> stable redacted `JOB_LOG_URI_UNSUPPORTED` or `JOB_LOG_ACCESS_DENIED`.
  - supported URI missing object -> `JOB_LOG_NOT_FOUND` without raw backend exception or credentials.
  - oversized log -> at most `NHMS_LOG_TAIL_MAX_BYTES` returned with bounded/truncation indication when schema allows.
  - compute-side production job emission -> newly recorded terminal/pipeline job log URI uses supported published URI or explicitly allowlisted S3 URI.
  - dev/local legacy logs with explicit allow flag -> existing safe behavior preserved; display with `NHMS_DISPLAY_ALLOW_LOCAL_FILE_LOGS=false` rejects private local paths.

Boundary-surface checklist:

- Shared helper roots: new `services/artifacts` parser/config/reader, existing `packages/common.safe_fs`, existing object-store/storage helpers if reused.
- Public entrypoints: job logs route and compute-side pipeline job submission/status update paths that write log URI.
- Read surfaces: published root files, allowlisted S3 objects, legacy local `LOG_ROOT` only under explicit allow conditions.
- Write/delete/overwrite surfaces: compute-side log persistence/copy into published namespace if implemented; no delete/overwrite behavior should be added to display reader.
- Staging/publish/rollback surfaces: gateway log fetch persistence and any object-store writes for canonical published logs.
- Producer/consumer evidence boundaries: `ops.pipeline_job.log_uri`, API error envelope, OpenAPI/generated types, future `/ops` log UI.
- Stale-state/idempotency boundaries: old job rows with private/unsupported `log_uri` must fail safely; repeated reads must not mutate artifacts or DB.
- Unchanged downstream consumers: existing monitoring API tests, orchestrator chain tests, retry/cancel tests, and frontend generated API consumers.

Required evidence:

- `uv run pytest -q tests/test_artifact_reader.py tests/test_pipeline_logs_artifacts.py tests/test_monitoring_api.py tests/test_api_contract.py tests/test_openapi_drift.py`: artifact URI safety, API error mapping, compute-side log URI emission, legacy compatibility, OpenAPI/contract drift.
- `uv run ruff check services/artifacts apps/api services/orchestrator tests/test_artifact_reader.py tests/test_pipeline_logs_artifacts.py`: style/static verification for touched backend paths.
- Frontend API type generation/check if OpenAPI/static schema changes are made; otherwise explicit no-op rationale with `git diff --quiet -- openapi/nhms.v1.yaml apps/frontend/src/api/types.ts`.
- If S3 support is implemented through a pluggable reader rather than real AWS credentials, tests must use a deterministic mock/stub and prove unallowlisted S3 does not call the reader.

Non-goals:

- Do not implement Docker mounts, compose files, systemd units, or two-node E2E evidence; those are later M22 issues.
- Do not implement frontend `/ops` log UI rendering or diagnostic copy; that is #235.
- Do not migrate historical DB rows in place; old unsupported/private rows must fail safely unless explicitly normalized by this PR.
- Do not add real cloud credentials or network-dependent tests.

## Migration Plan

1. Add service role config and conditionally mount Slurm routes. Keep `dev_monolith` default for existing local tests.
2. Add display-mode retry/cancel fail-closed backend behavior and tests proving no gateway or DB writes.
3. Add ArtifactReader and migrate pipeline log route to published artifact reads.
4. Add strict latest-product identity filters and update API/frontend contracts.
5. Make `/ops` display-mode UI read-only with diagnostic copy and manual 22 guidance.
6. Add Docker env/compose skeleton after role and read-only boundaries are implemented.
7. Add `Dockerfile.app` and entrypoint that select role commands without installing Slurm client by default.
8. Add readonly DB permission-denied validation before Docker deployment assets are considered ready.
9. Add systemd units and Docker/two-node E2E runbook, including scratch/evidence paths and disk preflight.
10. Add Docker security checks and strict cross-plane identity gates.

Rollback is staged: each PR is additive or role-gated. If Docker deployment blocks, keep role boundaries and continue running non-Docker services. If strict latest filters expose data gaps, source-only browsing remains available while cross-plane E2E stays blocked rather than falsely passing.

## Open Questions

- Whether the independent Slurm Gateway ASGI app should be added in this phase or documented as a follow-up after the 22 host systemd service path is proven. Until that decision is implemented, the `slurm_gateway` role is a reserved/host-service role and must not start the full business API by accident.

## Issue #232 Fixture: Latest-Product Strict Run Identity Contract

Fixture level: expanded
Repair intensity: high
Project profile: other

Change surface:

- `GET /api/v1/mvp/qhh/latest-product` query contract in `apps/api/routes/forecast.py`.
- Latest-product selection and unavailable diagnostics in `packages/common/forecast_store.py`.
- Static OpenAPI and generated frontend type surfaces touched by the added query parameters and response/error details.
- Backend tests for source-only compatibility, strict identity success, strict mismatch unavailable, partial strict identity rejection, normalization, no historical fallback, and contract drift.

Must preserve:

- Existing source-only latest-product browsing remains backward compatible and can still select the newest ready QHH product for a source.
- Existing ready product response fields remain available: `run_id`, `source_id`, `cycle_time`, `model_id`, `basin_id`, `basin_version_id`, `river_network_version_id`, `forcing_version_id`, counts, quality, and availability.
- Existing unsupported source validation and bounded reflected error details remain stable.
- Existing latest-product readiness checks for station/river coverage, identity alignment, and query-index diagnostics remain unchanged.

Must add/change:

- Add optional query parameters `run_id`, `cycle_time`, and `model_id` alongside required `source`.
- If any strict identity field is present, require all four fields: `source`, `run_id`, `cycle_time`, and `model_id`.
- Strict identity lookup must match all four fields exactly after existing source/cycle normalization and must never fall back to another run, older historical latest, or same-source/cycle sibling model.
- Partial strict identity requests return HTTP `422` with the standard validation error envelope, code `VALIDATION_ERROR`, safe details containing `missing_fields`, `required_fields`, `provided_fields`, and `strict_identity_required=true`, and no source-only store lookup.
- Strict mismatch must return `QHH_LATEST_PRODUCT_UNAVAILABLE` with safe requested identity details and unavailable reasons; it must not return a ready product for a different identity.
- Ready and unavailable responses must include enough requested/actual identity detail for later cross-plane E2E to prove the 27 display consumed the 22 run identity.
- Update OpenAPI, generated frontend types, API contract tests, and OpenAPI drift tests.

Selected risk packs:

- Public API / CLI / script entry: selected - extends public latest-product query contract and error behavior.
- Config / project setup: not selected - no new runtime env, deployment, or role config.
- File IO / path safety / overwrite: not selected - no filesystem or object-store reads/writes.
- Schema / columns / units / field names: selected - query params, response identity fields, OpenAPI/static types, and error details change.
- Geospatial / CRS / shapefile sidecars: not selected - no geospatial file handling.
- Time series / forcing / temporal boundaries: selected - strict `cycle_time` parsing/normalization and no historical fallback are central.
- Numerical stability / conservation / NaN: not selected - no numerical calculation changes.
- Solver runtime / performance / threading: not selected - no solver runtime behavior.
- Resource limits / large input / discovery: selected - latest candidate query must remain bounded and strict lookup must not perform unbounded scans.
- Legacy compatibility / examples: selected - source-only latest-product behavior and existing consumers must stay compatible.
- Error handling / rollback / partial outputs: selected - strict mismatch and partial identity must return stable typed errors without ambiguous success.
- Release / packaging / dependency compatibility: selected - cross-plane E2E and frontend generated types depend on the contract.
- Documentation / migration notes: not selected - frontend/runbook consumption is owned by later issues.

Invariant Matrix:

- Governing invariant: A strict latest-product request either returns the ready QHH product whose `source/run_id/cycle_time/model_id` exactly match the requested identity, or returns a typed unavailable/validation error; it must never silently substitute historical latest data.
- Source-of-truth identity/contract: request query parameters `source`, `run_id`, `cycle_time`, and `model_id`, normalized to the store's `source_id` and timestamp representation before SQL selection.
- Producers: 22 run evidence and hydro run rows that carry `run_id`, `source_id`, `cycle_time`, `model_id`, forcing/model/basin identity.
- Validators/preflight: FastAPI query validation, partial strict identity guard, source normalization, cycle-time parsing, strict SQL predicates, unavailable reason construction.
- Storage/cache/query: latest-product candidate query over hydro/model/forcing/station/river rows, bounded candidate/context limits, and no source-only fallback in strict mode.
- Public routes/entrypoints: `GET /api/v1/mvp/qhh/latest-product`.
- Frontend/downstream consumers: generated API types and future `/hydro-met` strict bootstrap / cross-plane E2E consumers.
- Failure paths/rollback/stale state: partial strict identity, unsupported source, malformed cycle time, no exact match, not-ready exact match, and same-source/cycle sibling mismatch all return stable errors.
- Evidence/audit/readiness: focused forecast API/store tests, API contract/OpenAPI drift tests, generated frontend type diff, and SQL capture proving strict predicates.
- Regression rows:
  - source-only request for `GFS` -> existing latest selection can skip newer unusable candidates and return newest ready source product.
  - strict request with matching `source/run_id/cycle_time/model_id` -> returns ready product whose identity fields match all four values.
  - strict request for an older ready run while a newer ready run exists -> returns the requested older run, not the historical latest.
  - strict request with wrong `run_id`, `cycle_time`, `source`, or `model_id` -> returns `QHH_LATEST_PRODUCT_UNAVAILABLE` with requested identity and no fallback product.
  - partial strict identity -> returns `422 VALIDATION_ERROR` before store lookup with `missing_fields`, `required_fields`, `provided_fields`, `strict_identity_required=true`, and does not call source-only latest selection.
  - same source/cycle with sibling model or run -> strict query cannot return the sibling.
  - unsupported or overlong source -> existing validation behavior and redaction remain stable.
  - OpenAPI/generated types expose `run_id`, `cycle_time`, and `model_id` query params while source-only remains valid.

Boundary-surface checklist:

- Shared helper roots: latest-product source/cycle normalization and candidate response/unavailable helpers in `packages/common/forecast_store.py`.
- Public entrypoints: `apps/api/routes/forecast.py::get_qhh_latest_product`.
- Read surfaces: hydro run/product candidate SQL and related model/forcing/station/river joins.
- Write/delete/overwrite surfaces: none.
- Staging/publish/rollback surfaces: none.
- Producer/consumer evidence boundaries: request identity, ready product identity, unavailable requested identity details, OpenAPI/generated types, future cross-plane E2E evidence.
- Stale-state/idempotency boundaries: strict lookup must not reuse stale source-only latest or cached historical latest results.
- Unchanged downstream consumers: existing source-only API tests, API contract tests, and generated type consumers.

Required evidence:

- `uv run pytest -q tests/test_forecast_api.py tests/test_api_contract.py tests/test_openapi_drift.py`: source-only compatibility, strict identity success/mismatch/partial rejection, no fallback, OpenAPI/contract drift.
- `uv run ruff check apps/api packages/common tests/test_forecast_api.py tests/test_api_contract.py`: style/static verification for touched backend paths.
- Frontend API type generation/check command used by the repo after expected OpenAPI/generated type changes are made; commit those schema/type updates, then verify there is no additional uncommitted drift with `git diff --quiet -- openapi/nhms.v1.yaml apps/frontend/src/api/types.ts`.
- SQL capture tests must prove strict predicates include all four identity fields and remain bounded.

Non-goals:

- Do not implement `/hydro-met` frontend bootstrap or E2E identity-file consumption; that is #235.
- Do not implement `/ops` stages/jobs/logs strict identity filtering; that is #233.
- Do not implement readonly DB validation, Docker compose/image/systemd, or final cross-plane E2E evidence; those are later issues.
- Do not change QHH readiness math, station/river aggregation semantics, or historical source-only browsing semantics except to share safe helper code.

## Issue #233 Fixture: Pipeline Ops Strict Run Identity Filters

Fixture level: expanded
Repair intensity: high
Project profile: other

Change surface:

- Pipeline monitoring query contract in `apps/api/routes/pipeline.py` for `GET /api/v1/pipeline/status`, `GET /api/v1/pipeline/stages`, `GET /api/v1/jobs`, and `GET /api/v1/jobs/{job_id}/logs`.
- Pipeline store/query helper logic used to resolve or validate `source`, `cycle_time`, `run_id`, and `model_id` together.
- Static OpenAPI, runtime OpenAPI patch if present, and generated frontend API types touched by the added query parameters or response metadata.
- Backend tests for source/cycle compatibility, strict identity success, duplicate same-source/cycle run/model mismatch, job/log evidence binding, and contract drift.

Must preserve:

- Existing source/cycle monitoring behavior remains compatible for non-strict browsing and current `/ops` consumers.
- Existing pipeline status, stages, jobs page, job log success envelope, pagination, sorting, status filtering, stage filtering, run type/scenario filtering, and published log error behavior remain stable.
- Existing display retry/cancel fail-closed behavior from #230 and published log URI safety from #231 are not weakened.
- Existing pipeline stage aggregation semantics for legacy jobs without `run_id` remain compatible when strict run identity is not requested.

Must add/change:

- Add optional strict identity inputs `run_id` and `model_id` alongside existing `source` and `cycle_time` where needed for pipeline status/stages/jobs/log evidence.
- If any strict identity field is present for ops evidence, all four fields `source`, `cycle_time`, `run_id`, and `model_id` are required.
- Strict ops identity must match a concrete `hydro.hydro_run` row after documented source/cycle normalization and must not fall back to source/cycle-only monitoring data.
- Strict pipeline status and stage responses must be scoped to the selected run identity or include stable mismatch/unavailable metadata that lets consumers reject wrong-run evidence.
- Strict jobs and logs must only return evidence for jobs matching the selected `run_id` and `model_id`; a job from the same source/cycle but different run/model must not satisfy strict evidence.
- Partial strict identity returns `422 VALIDATION_ERROR` with safe `missing_fields`, `provided_fields`, `required_fields`, and `strict_identity_required=true`, and must not perform source/cycle-only evidence lookup.
- Strict status/stages/jobs identity resolution failure returns HTTP `404` with code `PIPELINE_STRICT_IDENTITY_NOT_FOUND`, safe `requested_identity`, `strict_identity=true`, and a `reason` such as `run_not_found` or `run_model_cycle_source_mismatch`; it must not return source/cycle-only evidence.
- Strict job or log identity mismatch returns HTTP `409` with code `PIPELINE_STRICT_IDENTITY_MISMATCH`, safe `requested_identity`, safe `actual_identity` when available, `job_id` when applicable, `strict_identity=true`, and no mixed-run jobs or logs.
- Strict log identity mismatch must be rejected before constructing or invoking `ArtifactReader.read_text_tail()` so the wrong job's published log content is never read.
- Update OpenAPI, generated frontend types, API contract tests, and OpenAPI drift tests.

Strict ops identity error contract:

- `422 VALIDATION_ERROR`: any of `source`, `cycle_time`, `run_id`, or `model_id` is missing or blank when another strict identity field is present. Required details: `missing_fields`, `provided_fields`, `required_fields=["source","cycle_time","run_id","model_id"]`, `strict_identity_required=true`, and bounded `rejected_values` for supplied blank/overlong values when applicable.
- `422 VALIDATION_ERROR`: unsupported source or malformed/date-only `cycle_time`. Required details include bounded `field` and `rejected_value`, matching existing monitoring validation style where possible.
- `404 PIPELINE_STRICT_IDENTITY_NOT_FOUND`: no `hydro.hydro_run` row matches all four normalized strict identity fields. Required details: `strict_identity=true`, `requested_identity` with safe bounded `source`, `source_id`, `cycle_time`, `run_id`, and `model_id`, plus `reason="run_not_found"`.
- `404 PIPELINE_STRICT_IDENTITY_NOT_FOUND`: a source/cycle forecast cycle exists but the requested run/model is not part of that source/cycle identity. Required details: same as above, plus `reason="run_model_cycle_source_mismatch"` and bounded `candidate_count` or `available_run_ids_sample` only when safe and capped.
- `409 PIPELINE_STRICT_IDENTITY_MISMATCH`: a strict `/jobs` or `/jobs/{job_id}/logs` request encounters a job whose `run_id`/`model_id` does not match the requested identity. Required details: `strict_identity=true`, safe `requested_identity`, safe `actual_identity` with `job_id`, `run_id`, `model_id`, `cycle_id`, and any resolved `source_id/cycle_time` when available, plus `reason="job_identity_mismatch"`.
- Log mismatch rejection order: `PIPELINE_STRICT_IDENTITY_MISMATCH` must be raised before any published log read or artifact reader call. `JOB_NOT_FOUND`, `JOB_LOG_NOT_PUBLISHED`, `JOB_LOG_URI_UNSUPPORTED`, `JOB_LOG_ACCESS_DENIED`, and `JOB_LOG_NOT_FOUND` remain the log-specific errors only after the job identity has been validated.

Selected risk packs:

- Public API / CLI / script entry: selected - extends public monitoring status/stages/jobs/logs query contracts.
- Config / project setup: not selected - no new runtime env or deployment settings.
- File IO / path safety / overwrite: selected - job logs are a read surface and must preserve #231 published artifact safety when strict identity is added.
- Schema / columns / units / field names: selected - new query params, response metadata/error details, OpenAPI/static types, and hydro/job identity fields are in scope.
- Geospatial / CRS / shapefile sidecars: not selected - no geospatial file handling.
- Time series / forcing / temporal boundaries: selected - strict `cycle_time` parsing/normalization and source/cycle duplicate handling are central.
- Numerical stability / conservation / NaN: not selected - no numerical calculations.
- Solver runtime / performance / threading: not selected - no solver runtime behavior.
- Resource limits / large input / discovery: selected - monitoring queries, stage samples, and job pages must remain bounded under strict filters.
- Legacy compatibility / examples: selected - source/cycle browsing and legacy job rows must remain compatible outside strict mode.
- Error handling / rollback / partial outputs: selected - strict mismatch and partial identity must return stable typed errors without ambiguous evidence.
- Release / packaging / dependency compatibility: selected - `/ops`, cross-plane E2E, OpenAPI, and generated frontend types depend on the contract.
- Documentation / migration notes: not selected - frontend UI/runbook docs are owned by later issues.

Invariant Matrix:

- Governing invariant: Ops status, stages, jobs, and log evidence for a strict request must be bound to one concrete `source/cycle_time/run_id/model_id` identity, or fail with a typed validation/unavailable error; it must never mix jobs/logs from a same-source/cycle sibling run or silently fall back to source/cycle-only evidence.
- Source-of-truth identity/contract: request query parameters `source`, `cycle_time`, `run_id`, and `model_id`, normalized to the store's `source_id` and timestamp representation, then resolved against `hydro.hydro_run` and `ops.pipeline_job`.
- Producers: 22 run evidence, `hydro.hydro_run` rows, `met.forecast_cycle` rows, `ops.pipeline_job` rows, and published artifact log URIs.
- Validators/preflight: FastAPI query validation, partial strict identity guard, source normalization, cycle-time parsing, hydro run identity lookup, job/log identity match checks, and safe error detail construction.
- Storage/cache/query: forecast-cycle lookup, hydro-run identity lookup, pipeline job status/stage/job queries, published log reads, bounded stage sample/job pagination limits, and no source/cycle-only fallback in strict mode.
- Public routes/entrypoints: `GET /api/v1/pipeline/status`, `GET /api/v1/pipeline/stages`, `GET /api/v1/jobs`, `GET /api/v1/jobs/{job_id}/logs`.
- Frontend/downstream consumers: generated API types and future `/ops` strict identity UI/cross-plane E2E consumers.
- Failure paths/rollback/stale state: partial strict identity, unsupported source, malformed cycle time, no matching hydro run, same-source/cycle sibling run/model mismatch, job/log identity mismatch, and legacy rows missing identity all return stable errors or explicit non-strict compatibility behavior.
- Evidence/audit/readiness: focused monitoring API tests, API contract/OpenAPI drift tests, generated frontend type diff, duplicate same-source/cycle fixtures, and published-log strict identity checks.
- Regression rows:
  - source/cycle request without strict fields -> existing status/stages/jobs/log browsing remains compatible.
  - strict request with matching `source/cycle_time/run_id/model_id` -> status/stages/jobs/log evidence is scoped to the selected run identity.
  - strict request when a sibling run shares source/cycle but has different run/model -> sibling jobs/logs cannot satisfy the strict request.
  - strict status/stages for nonexistent run/model -> `404 PIPELINE_STRICT_IDENTITY_NOT_FOUND` with safe requested identity and no source/cycle fallback.
  - partial strict identity -> `422 VALIDATION_ERROR` before source/cycle-only lookup with safe missing/provided/required details.
  - strict `GET /api/v1/jobs/{job_id}/logs` for a job not matching requested run/model -> `409 PIPELINE_STRICT_IDENTITY_MISMATCH` and no log read attempt.
  - unsupported or overlong source / malformed cycle_time -> existing validation and bounded reflection remain stable.
  - legacy jobs without run identity -> remain visible in non-strict source/cycle browsing but cannot satisfy strict run evidence.

Boundary-surface checklist:

- Shared helper roots: pipeline source/cycle parsing, hydro-run identity lookup, monitoring query helpers, job payload/log helpers, and published log reader integration.
- Public entrypoints: pipeline status, stages, jobs, and job logs routes.
- Read surfaces: `met.forecast_cycle`, `hydro.hydro_run`, `ops.pipeline_job`, published log artifacts.
- Write/delete/overwrite surfaces: none - this issue must not add writes or mutate pipeline state.
- Staging/publish/rollback surfaces: none.
- Producer/consumer evidence boundaries: requested identity, resolved run identity, job payload identity, log request identity, OpenAPI/generated types, future `/ops` and cross-plane E2E evidence.
- Stale-state/idempotency boundaries: repeated strict reads must be side-effect free; stale source/cycle jobs must not be used as strict evidence for a sibling run.
- Unchanged downstream consumers: existing monitoring API tests, frontend generated API consumers, retry/cancel fail-closed tests, and published log tests.

Required evidence:

- `uv run pytest -q tests/test_monitoring_api.py tests/test_api_contract.py tests/test_openapi_drift.py`: strict ops identity success/mismatch/partial validation, named `PIPELINE_STRICT_IDENTITY_NOT_FOUND` and `PIPELINE_STRICT_IDENTITY_MISMATCH` errors/details, source/cycle compatibility, jobs/logs evidence binding, OpenAPI/contract drift.
- `uv run ruff check apps/api tests/test_monitoring_api.py tests/test_api_contract.py tests/test_openapi_drift.py`: style/static verification for touched backend paths.
- Frontend API type generation/check after OpenAPI/generated type changes; commit those schema/type updates and verify there is no additional uncommitted drift with `git diff --quiet -- openapi/nhms.v1.yaml apps/frontend/src/api/types.ts`.
- If strict log identity validation touches published artifact reading, include focused log tests proving `PIPELINE_STRICT_IDENTITY_MISMATCH` rejects before `ArtifactReader.read_text_tail()` and safe published-log behavior remains intact.

Non-goals:

- Do not implement frontend `/ops` rendering, diagnostic copy, hidden controls, or browser tests; that is #235.
- Do not change latest-product strict identity; that is #232 and already landed.
- Do not implement readonly DB validation, Docker compose/image/systemd, or final cross-plane E2E evidence; those are later issues.
- Do not change retry/cancel mutation behavior except preserving #230 compatibility.

## Issue #234 Fixture: Readonly DB Boundary Validation

Fixture level: expanded
Repair intensity: high
Project profile: other

Change surface:

- A readonly DB validation entrypoint, tests, or runbook command that can exercise the display API with readonly credentials and write structured evidence under `artifacts/` or `/scratch/frd_muziyao`.
- Database permission probes for hydro, met, ops, and pipeline-critical tables, including `INSERT`, `UPDATE`, `DELETE`, and DDL attempts that are rejected by DB permissions or readonly transaction semantics before commit.
- Display-mode retry/cancel validation proving `CONTROL_PLANE_MANUAL_ACTION_REQUIRED` is returned before any write attempt when readonly credentials are used.
- Tests and evidence helpers for secret redaction, `current_user`, DB role type, redacted DSN, command list, pass/fail/blocker status, and fixture skip/block behavior when a real DB is unavailable.

Must preserve:

- Existing display retry/cancel fail-closed behavior from #230, including auth/RBAC ordering and no gateway construction in display mode.
- Existing published artifact log safety from #231 and strict latest-product / ops identity behavior from #232 and #233.
- Existing normal test runs must not require a real PostgreSQL server unless the readonly DB validation env is explicitly enabled.
- Existing application route contracts and OpenAPI/type surfaces remain unchanged unless a validation endpoint is explicitly added, which is not expected for this issue.

Must add/change:

- Add a focused readonly DB validation command or pytest entrypoint that records a structured evidence object with `current_user`, DB role type, redacted DB URL, command/probe results, route smoke results, and final `PASS` / `FAIL` / `BLOCKED`.
- Route smoke must cover display-safe reads for health, runtime config, models, stations or station-equivalent read APIs, latest-product, pipeline status, stages, jobs, job logs, and any required setup preconditions.
- Permission probes must prove controlled writes and DDL are rejected before commit for representative hydro, met, ops, and pipeline-critical surfaces. Rollback is cleanup only; any DML or DDL probe that executes successfully under the tested credential is `FAIL`, even if it is later rolled back.
- Retry/cancel probes must run with `NHMS_SERVICE_ROLE=display_readonly` and prove manual-action `409` is returned before any DB write is attempted.
- Evidence must redact secrets from DSNs, connection strings, errors, and command output; project-created evidence and temporary files must stay under the repository `artifacts/` tree or `/scratch/frd_muziyao`.
- When no real readonly DB URL is configured, the validation command must produce an explicit `BLOCKED` / skipped result rather than a false pass.

Selected risk packs:

- Public API / CLI / script entry: selected - adds a validation command/test entrypoint and exercises public display routes.
- Config / project setup: selected - depends on explicit readonly DB connection env, `NHMS_SERVICE_ROLE=display_readonly`, and safe skip/block semantics.
- File IO / path safety / overwrite: selected - evidence artifacts and temporary outputs must stay under approved roots and avoid leaking secrets.
- Schema / columns / units / field names: selected - permission probes target named schemas/tables and evidence has a structured schema.
- Geospatial / CRS / shapefile sidecars: not selected - no geometry or shapefile processing changes.
- Time series / forcing / temporal boundaries: selected - latest-product, station, and pipeline route smoke may require cycle/source identity and timestamp fixtures.
- Numerical stability / conservation / NaN: not selected - no solver or numerical behavior.
- Solver runtime / performance / threading: not selected - no solver runtime behavior.
- Resource limits / large input / discovery: selected - validation must bound route smoke, probes, evidence size, and DB work; no broad destructive scans.
- Legacy compatibility / examples: selected - normal dev/test flows and existing writer-credential integration tests must remain compatible.
- Error handling / rollback / partial outputs: selected - failed probes must roll back, produce stable evidence, and never leave partial DB writes.
- Release / packaging / dependency compatibility: selected - later Docker/display deployment uses this validation as a readiness gate.
- Documentation / migration notes: selected - runbook or command documentation must explain env, redaction, evidence path, and blocked/pass/fail semantics.

Invariant Matrix:

- Governing invariant: A display readonly DB validation pass means the API can read required display surfaces with readonly credentials, cannot write hydro/met/ops/pipeline-critical state, and retry/cancel fail closed before any DB mutation; writer credentials or missing evidence must not be mislabeled as readonly PASS.
- Source-of-truth identity/contract: readonly DB connection env, `current_user`, DB role attributes/privileges, `NHMS_SERVICE_ROLE=display_readonly`, and the structured validation evidence schema.
- Producers: validation command/test harness, DB role introspection queries, route smoke responses, permission probe results, and retry/cancel manual-action responses.
- Validators/preflight: env/config validation, DSN redaction, approved evidence-root selection, display runtime config check, DB role/user query, permission-denied classifier, transaction rollback handling.
- Storage/cache/query: PostgreSQL schemas/tables for hydro, met, ops, pipeline jobs/events, latest-product read surfaces, monitoring read surfaces, and published log rows.
- Public routes/entrypoints: `/health`, `/api/v1/runtime/config`, models/stations read routes, `/api/v1/mvp/qhh/latest-product`, `/api/v1/pipeline/status`, `/api/v1/pipeline/stages`, `/api/v1/jobs`, `/api/v1/jobs/{job_id}/logs`, retry, and cancel.
- Frontend/downstream consumers: later Docker/E2E gates and runbooks that consume readonly validation evidence; no frontend code is owned by this issue.
- Failure paths/rollback/stale state: missing DB env, writer credentials, unavailable fixture data, route failures, permission probes unexpectedly succeeding, retry/cancel attempting writes, secret redaction failures, and evidence write failures.
- Evidence/audit/readiness: structured evidence file, focused pytest/CLI tests, readonly DB smoke command output, redacted DSN/current user, probe matrix, and CI or local skip/block status.
- Regression rows:
  - readonly DB env absent -> validation reports `BLOCKED` or pytest skip, not `PASS`.
  - readonly credential with display role -> display read routes succeed or report explicit fixture blockers without DB writes.
  - writer credential labeled readonly -> write/DDL probes succeed and validation returns `FAIL`, never `PASS`.
  - readonly credential write/DDL probes -> permission-denied or readonly-transaction errors recorded for hydro/met/ops/pipeline-critical surfaces before commit.
  - any tested credential can execute DML/DDL successfully -> validation returns `FAIL`, even when the harness rolls back for cleanup.
  - display retry/cancel with readonly credentials -> `CONTROL_PLANE_MANUAL_ACTION_REQUIRED` before write/gateway side effects.
  - evidence containing DSN/password/token-like values -> secrets redacted before file write and assertion output.
  - normal local unit test run without readonly DB env -> existing backend tests remain runnable without external DB.

Boundary-surface checklist:

- Shared helper roots: DB engine/session setup, validation evidence writer/redactor, permission probe helpers, display route smoke helpers.
- Public entrypoints: validation CLI/pytest entrypoint and display API routes exercised by the smoke.
- Read surfaces: health/runtime config, model/station/latest-product/pipeline/job/log read APIs.
- Write/delete/overwrite surfaces: controlled DB `INSERT`, `UPDATE`, `DELETE`, DDL probes inside rollback-safe transactions, where successful execution is a failure and rollback is only cleanup.
- Staging/publish/rollback surfaces: evidence file creation and DB transaction rollback for probes.
- Producer/consumer evidence boundaries: redacted evidence schema, local artifact path, CI/log output, future Docker/E2E consumers.
- Stale-state/idempotency boundaries: repeated validation runs must not accumulate writes, reuse stale PASS evidence, or depend on dirty DB state without reporting blockers.
- Unchanged downstream consumers: existing API tests, migration tests, retry/cancel tests, latest-product tests, and monitoring tests.

Required evidence:

- Focused readonly DB validation command or pytest target added by this issue: readonly env -> route smoke and permission probe matrix with redacted evidence.
- Minimum permission probe matrix: `hydro.hydro_run`, `hydro.river_timeseries`, `met.forecast_cycle`, `met.forcing_station_timeseries` or the available station-equivalent table, `ops.pipeline_job`, `ops.pipeline_event`, plus at least one schema-level or table-level DDL probe. If a table is absent in a reduced fixture, evidence must mark that row `BLOCKED` or out-of-scope with a concrete reason rather than silently skipping it.
- `uv run pytest -q tests/test_monitoring_api.py tests/test_retry_cancel_consistency.py tests/test_forecast_api.py`: display fail-closed, logs, latest-product, and monitoring compatibility.
- `uv run pytest -q <new readonly validation tests>`: evidence schema, redaction, blocked/skip behavior, writer-credential FAIL simulation, permission-denied classification, rollback/idempotency.
- `uv run ruff check tests services apps/api`: style/static verification for touched validation code.
- If the validation command writes evidence, inspect generated sample evidence under `artifacts/` or `/scratch/frd_muziyao` and prove no secrets or unapproved output paths are present.

Non-goals:

- Do not implement Docker compose/image/systemd or container HostConfig checks; those are #236 through #239.
- Do not implement frontend `/ops` or `/hydro-met` behavior; that is #235.
- Do not create production DB roles, grant/revoke privileges, or require privileged DDL outside controlled probes.
- Do not seed or mutate production-like data as part of a PASS; missing fixture data should be a blocker or reduced-scope result, not an unsafe write.
