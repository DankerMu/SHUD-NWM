## ADDED Requirements

### Requirement: Repository paths have a documented four-role owner

The repository SHALL document active paths under one of four categories: `compute_control`, `display_readonly`, `slurm_gateway`, or `shared_contract`. Each category MUST include allowed mutations, forbidden capabilities, verification oracle, and representative paths.

#### Scenario: maintainer classifies an active path
- **WHEN** a maintainer opens `docs/governance/ROLE_BOUNDARY.md`
- **THEN** they can determine whether a path belongs to `compute_control`, `display_readonly`, `slurm_gateway`, or `shared_contract`

#### Scenario: shared contract is not misclassified as a node-specific path
- **WHEN** a path such as `packages/common`, `openapi`, `schemas`, or `db/migrations` is listed
- **THEN** it is classified as `shared_contract`, not as exclusively node-22 or node-27

### Requirement: display_readonly cannot gain control-plane capability

The `display_readonly` role SHALL remain a readonly display plane. It MUST NOT register Slurm routes, configure compute-only env, enable control mutations, or perform retry/cancel/queue-depth control actions.

#### Scenario: display service starts with Slurm gateway env
- **WHEN** `NHMS_SERVICE_ROLE=display_readonly` and `SLURM_GATEWAY_URL` or `SLURM_GATEWAY_BACKEND` is present
- **THEN** startup validation fails before serving requests

#### Scenario: display service starts with compute-only path or runtime env
- **WHEN** `NHMS_SERVICE_ROLE=display_readonly` and compute-only env such as `WORKSPACE_ROOT`, `OBJECT_STORE_ROOT`, `SHUD_EXECUTABLE`, `SLURM_GATEWAY_TEMPLATE_DIR`, `SLURM_GATEWAY_WORKSPACE_DIR`, Docker socket configuration, or enabled control mutation flags are present
- **THEN** startup validation fails before serving requests or static compose/env validation reports the configuration as forbidden

#### Scenario: display API receives retry or cancel
- **WHEN** a retry or cancel endpoint is called under `display_readonly`
- **THEN** the response is `409 CONTROL_PLANE_MANUAL_ACTION_REQUIRED`

#### Scenario: display API receives queue-depth request
- **WHEN** queue depth is requested under `display_readonly`
- **THEN** the response is `503 CONTROL_PLANE_QUEUE_UNAVAILABLE`

### Requirement: standalone Slurm gateway remains bounded

The standalone Slurm gateway app SHALL expose only Slurm gateway routes and MUST NOT expose forecast, model, pipeline, static, or frontend routes.

#### Scenario: gateway app route inventory is inspected
- **WHEN** the standalone gateway app is built
- **THEN** route inventory includes `/api/v1/slurm/*` and excludes business API/frontend routes

### Requirement: production orchestration does not depend on QHH diagnostic scripts

Production scheduler/orchestrator code SHALL NOT call or import `scripts/run_qhh_continuous.py`, `scripts/run_qhh_cycle.sh`, or related diagnostic-only script tokens.

#### Scenario: production orchestration files are scanned
- **WHEN** static boundary tests scan production orchestrator modules
- **THEN** no QHH diagnostic script token appears in production scheduler/orchestrator code

### Requirement: Shared contract code does not depend upward on API modules

Shared contract packages, workers, and orchestrator modules SHALL NOT import `apps.api.*` for policy evidence or authorization helpers. Shared policy helpers required outside the API layer MUST live in a shared package, with API-only request dependencies kept in `apps/api`.

#### Scenario: shared and worker imports are scanned
- **WHEN** static boundary tests scan `packages/common`, `services/orchestrator`, and `workers/**`
- **THEN** imports from `apps.api.*` are absent, except for a temporary issue-scoped allowlist that points to the shared auth/policy extraction issue

#### Scenario: shared policy helper extraction lands
- **WHEN** CLI, worker, or shared package code needs policy evidence helpers
- **THEN** it imports those helpers from a shared package rather than `apps.api.auth`
