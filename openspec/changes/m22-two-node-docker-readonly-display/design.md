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
