# Role Boundary Inventory

This inventory is the repository source of truth for the current NHMS role
boundary. The governing invariant is: runtime role determines allowed
control-plane capability, and shared contracts must not depend upward on
API-layer helpers. #361 extracted shared auth policy helpers into
`packages/common/auth_policy.py`, so this boundary is now a hard gate.

`ServiceRole` in `apps/api/runtime_mode.py` is the runtime role source for
application startup. `shared_contract` is a governance category, not a
`ServiceRole` value. `dev_monolith` is local-development compatibility for the
full API and is not a production deployment category.

## Node Relation

**Current physical deployment (2026-06-21):**

- node-22 runs the Slurm/SHUD compute wrapper. It does **NOT** connect to any active
  database. The local PG `:55433` instance on node-22 is historical and pending
  removal — do not connect to it.
- node-27 hosts the active primary PostgreSQL (`:55432`), the ingest workers, the
  display API (`:8080`), and the frontend on a single machine. node-27 reads
  node-22's compute artifacts via NFS (`/home/ghdc/nwm/` ↔ node-22
  `/ghdc/data/nwm/`) and writes its own database directly.
- Public service entry: `https://test.nwm.ac.cn` (27 reverse-proxied).

**Design-time role contract (preserved below) describes capability boundaries
that the codebase still enforces; physical host assignment may differ.**

node-22 runs `compute_control` services and may also run the standalone
`slurm_gateway`. The `compute_control` role owns scheduler execution, Slurm
submission, writable workspace/object-store roots, database mutation, and
publication of display artifacts. The `display_readonly` role serves the
display API/frontend and reads the readonly database plus published artifacts.

> Note: in the current deployment the `compute_control` writes happen on
> node-27, not node-22. The role contract is what the code enforces; the host
> assignment is what ops has rolled out.

Shared contracts are not "node-22 code" or "node-27 code". Paths such as
`packages/common`, `openapi/nhms.v1.yaml`, `db/migrations`, `schemas`, and
generated API types define contracts used by both nodes. node-22 may produce or
apply those contracts during controlled deployment/publish flows, and node-27
may consume them, but the contracts remain `shared_contract`.

## `compute_control`

Representative active paths:

- `apps/api/main.py` when `NHMS_SERVICE_ROLE=compute_control`, including Slurm
  route registration through `services.slurm_gateway.routes`.
- `apps/api/routes/pipeline.py` control-plane retry, cancel, queue, run, and
  artifact endpoints.
- `services/orchestrator/cli.py`, `services/orchestrator/scheduler.py`,
  `services/orchestrator/chain.py`, `services/orchestrator/retry.py`,
  `services/orchestrator/reconcile.py`, and `services/orchestrator/persistence.py`.
- `workers/data_adapters`, `workers/forcing_producer`,
  `workers/canonical_converter`, `workers/shud_runtime`,
  `workers/output_parser`, `workers/flood_frequency`, and
  `workers/model_registry`.
- `services/tile_publisher` and `services/production_closure`.
- `infra/compose.compute.yml`, `infra/env/compute.example`, `infra/sbatch`,
  and compute-side systemd/runbook assets.

Legacy Slurm template note: `workers/sbatch_templates` was retired from the
active tree by #363. Legacy template names and migration notes are archived in
`docs/archived/legacy-slurm-templates.md`. The active real Slurm template
directory is `infra/sbatch`, matching `services/slurm_gateway/config.py` where
`SlurmGatewaySettings.template_dir` defaults to `infra/sbatch` and
`SLURM_GATEWAY_TEMPLATE_DIR` maps to that setting.

Allowed mutations:

- Use writer database credentials for model registry, pipeline lifecycle,
  retry/cancel, active model/version changes, scheduler reservation, and
  production closure evidence.
- Write workspace, scheduler lock/evidence/runtime/temp roots, object-store
  roots, published artifact roots, tiles, manifests, logs, and run evidence.
- Submit, inspect, and cancel Slurm jobs through the configured Slurm gateway.
- Apply shared migrations and publish shared artifacts as a controlled
  deployment action; the migration/schema contract itself remains
  `shared_contract`.

Forbidden capabilities:

- Running the full business API with `NHMS_SERVICE_ROLE=slurm_gateway`.
- Treating QHH diagnostic scripts (`scripts/run_qhh_*` or
  `scripts/create_qhh_shud_manifest.py`) as production orchestrator entrypoints.
- Adding new upward imports from `packages/common`, `services/**`,
  `workers/**`, or documented shared-contract Python files such as
  `services/slurm_gateway/models.py` into `apps.api` / `apps.api.*`.
- Depending on display-only environment to gain control mutations.

Verification oracle:

- `ServiceRole` and `RuntimeConfig` in `apps/api/runtime_mode.py`.
- Full API route inventory in `apps/api/main.py`.
- Compute env and compose identity in `infra/env/compute.example` and
  `infra/compose.compute.yml`.
- Recursive diagnostic-token scans of production Python sources under
  `services/orchestrator/**/*.py`.

Current guard tests:

- `tests/test_runtime_mode.py`
- `tests/test_two_node_docker_runtime.py`
- `tests/test_qhh_scripts_static.py`
- `tests/test_retry_cancel_consistency.py`
- `tests/test_monitoring_api.py`
- `tests/test_role_boundary_static.py`

## `display_readonly`

Representative active paths:

- `apps/api/main.py` when `NHMS_SERVICE_ROLE=display_readonly`.
- Readonly API routes in `apps/api/routes/forecast.py`,
  `apps/api/routes/models.py`, `apps/api/routes/pipeline.py`,
  `apps/api/routes/flood_alerts.py`, `apps/api/routes/best_available.py`,
  `apps/api/routes/data_sources.py`, and
  `apps/api/routes/state_snapshots.py`.
- Frontend display assets under `apps/frontend`.
- Display deployment inputs `infra/compose.display.yml`,
  `infra/env/display.example`, `infra/systemd/nhms-display-compose.service`,
  and display runbooks under `docs/runbooks`.

Allowed mutations:

- Serve readonly responses, frontend/static assets, MVT/display data, health,
  runtime config, and fail-closed error responses.
- Read the readonly database role, readonly published artifacts, and the shared
  object-store mirror through `OBJECT_STORE_ROOT` for display-only station
  forcing CSV reads. `OBJECT_STORE_ROOT` is a required/audited display runtime
  env for that read path and does not grant write or producer capability.

Forbidden capabilities:

- Registering `/api/v1/slurm/*` on the display API.
- Configuring `SLURM_GATEWAY_URL`, `SLURM_GATEWAY_BACKEND`,
  `SLURM_GATEWAY_TEMPLATE_DIR`, `SLURM_GATEWAY_WORKSPACE_DIR`,
  `WORKSPACE_ROOT`, `RUN_WORKSPACE_ROOT`, `SHARED_LOG_ROOT`,
  scheduler roots, `NHMS_BASINS_ROOT`,
  `NHMS_MODEL_ASSET_ROOT`, `SHUD_EXECUTABLE`, `MUNGE_SOCKET`, `MUNGE_KEY`, or
  `DOCKER_HOST`.
- Opting into control mutations by setting
  `NHMS_DISPLAY_DISABLE_CONTROL_MUTATIONS=false`.
- Performing retry/cancel actions or live queue-depth control-plane queries;
  these fail closed with display-specific error codes.
- Writing business state, scheduler state, Slurm state, workspace roots,
  object-store roots, producer/copyback roots, or published artifact roots.

Current residual / follow-up boundary:

- `display_readonly` currently still registers some non-Slurm mutation-shaped
  routes, including model registry and hindcast endpoints, that are protected by
  auth checks and the readonly DB/deployment posture rather than by the #360
  runtime role guard. Governance-1/Governance-4 follow-up work must decide
  whether to gate or split display mutation routes, or add display-wide
  fail-closed mutation tests. #360 only claims the display Slurm-route,
  compute-env, retry/cancel, and queue-depth boundaries above.

Verification oracle:

- `display_boundary_blockers()` and `load_runtime_config()` in
  `apps/api/runtime_mode.py`.
- Display route inventory from `apps/api/main.py:create_app()`.
- Display env/compose static checks in `scripts/validate_two_node_docker_runtime.py`.
- Display fail-closed error contracts in `apps/api/routes/pipeline.py`.

Current guard tests:

- `tests/test_runtime_mode.py`
- `tests/test_two_node_docker_runtime.py`
- `tests/test_retry_cancel_consistency.py`
- `tests/test_monitoring_api.py`
- `tests/test_role_boundary_static.py`

## `slurm_gateway`

Representative active paths:

- `services/slurm_gateway/app.py`
- `services/slurm_gateway/routes.py`
- `services/slurm_gateway/config.py`
- `services/slurm_gateway/gateway.py`
- `services/slurm_gateway/real_backend.py`
- `services/slurm_gateway/mock_backend.py`
- `services/slurm_gateway/models.py`
- `services/slurm_gateway/__main__.py`
- `infra/systemd/nhms-slurm-gateway.service`
- `infra/sbatch`

Allowed mutations:

- Submit Slurm jobs, submit Slurm job arrays, inspect job state, cancel jobs,
  and fetch logs through `/api/v1/slurm/*`.
- Maintain gateway-local/mock backend state.
- Register `/api/v1/slurm/internal/reset` only when
  `SLURM_GATEWAY_ALLOW_INTERNAL_RESET` is explicitly enabled; it is absent by
  default.

Forbidden capabilities:

- Serving forecast, model, pipeline, data-source, static, or frontend business
  routes.
- Serving sibling prefixes such as `/api/v1/slurmish` or
  `/api/v1/slurm-admin`; the standalone namespace is slash-delimited as
  `/api/v1/slurm` and `/api/v1/slurm/*`.
- Starting the full API as `NHMS_SERVICE_ROLE=slurm_gateway`.
- Mutating NHMS business database state or published artifact state directly.
- Becoming a general-purpose compute-control API.

Verification oracle:

- Standalone route inventory from
  `services.slurm_gateway.app:create_gateway_app()`, including non-APIRoute
  framework, mount, static, or frontend routes.
- Gateway settings in `services/slurm_gateway/config.py`.
- Full API fail-fast behavior for `ServiceRole.SLURM_GATEWAY`.

Current guard tests:

- `tests/test_slurm_gateway_app.py`
- `tests/test_runtime_mode.py`
- `tests/test_role_boundary_static.py`
- `tests/test_gateway.py`
- `tests/test_slurm_route_contract.py`

## `shared_contract`

Representative active paths:

- `packages/common`
- `openapi/nhms.v1.yaml`
- `apps/frontend/src/api/types.ts`
- `db/migrations`
- `schemas`
- `services/slurm_gateway/models.py`
- `infra/sbatch`
- `docs/modules/*_spec.md`

Allowed mutations:

- Define shared schemas, OpenAPI contracts, database migrations, run manifests,
  storage object contracts, source identity, model registry data structures,
  Slurm request/response models, and generated client types.
- Change contracts through explicit contract/migration work with corresponding
  tests and generated artifacts.

Forbidden capabilities:

- Depending upward on `apps.api.*` from shared packages, workers, or
  orchestrator code, or from documented shared-contract Python files such as
  `services/slurm_gateway/models.py`.
- Hiding role-specific control-plane actions inside common helpers.
- Changing `openapi/nhms.v1.yaml` or generated frontend API types as part of
  #360.
- Classifying shared contracts as exclusively node-22 or node-27 owned paths.

Verification oracle:

- AST import scan over `packages/common`, `services/**`, and `workers/**`,
  including documented shared-contract Python files such as
  `services/slurm_gateway/models.py` and the production-closure auth-policy
  evidence surface `services/production_closure/ops_validation.py`.
- OpenAPI drift, migration, schema, and static route tests.
- Review of generated/public artifacts before contract changes land.

Current guard tests:

- `tests/test_role_boundary_static.py`
- `tests/test_openapi_drift.py`
- `tests/test_migrations.py`
- `tests/test_api_contract.py`
- `tests/test_slurm_route_contract.py`

## #361 Hard Gate

`#361` moved the API-independent auth policy contract to
`packages/common/auth_policy.py`. Shared packages, service modules, workers,
documented shared-contract Python files, and
`services/production_closure/ops_validation.py` must have zero imports from
`apps.api` or `apps.api.*`.

The static gate normalizes parent-package spellings such as `import apps.api`,
`from apps.api import auth`, `from apps import api`, and wildcard imports that
resolve to `apps.api`. Any such import in the scanned shared/service/worker
surfaces fails `tests/test_role_boundary_static.py`.

API request handling remains in `apps/api/auth.py`: FastAPI `Request` parsing,
request-state audit recording, `require_action`, `evaluate_request_action`,
`auth_context_from_request`, and API error mapping stay API-owned. Readonly DB
validation may exercise display route smoke and retry/cancel probes, but FastAPI
app construction, `TestClient` ownership, and pipeline dependency overrides live
behind the API-owned `apps/api/readonly_validation_probe.py` adapter instead of
inside `services/production_closure/readonly_db_validation.py`.
