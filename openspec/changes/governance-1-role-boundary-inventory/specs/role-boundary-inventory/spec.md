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

The `display_readonly` role SHALL remain a display plane with fail-closed
control-plane guardrails in the #360 scope. It MUST NOT register Slurm routes,
configure compute-only env, enable control mutations, or perform
retry/cancel/queue-depth control actions.

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

### Requirement: display non-Slurm mutation residual is explicit

#360 SHALL NOT claim full display mutation route removal while
`display_readonly` still registers non-Slurm mutation-shaped routes. The
Governance-1 inventory MUST document that current model registry and hindcast
mutation-shaped routes are protected by auth checks and readonly DB/deployment
posture rather than by the #360 runtime role guard.

#### Scenario: maintainer reviews display mutation scope

- **WHEN** a maintainer reviews the Governance-1 role-boundary inventory
- **THEN** they can see that display non-Slurm mutation-shaped routes are
  residual follow-up scope for gating, route splitting, or display-wide
  fail-closed mutation tests

### Requirement: standalone Slurm gateway remains bounded

The standalone Slurm gateway app SHALL expose only slash-delimited Slurm gateway routes and explicit framework docs/openapi routes. It MUST NOT expose forecast, model, pipeline, static, frontend, mounted, or sibling-prefix routes.

#### Scenario: gateway app route inventory is inspected

- **WHEN** the standalone gateway app is built
- **THEN** route inventory includes `/api/v1/slurm` or `/api/v1/slurm/*`
- **AND** route inventory excludes business API/frontend/static routes, mounted routes, and sibling prefixes such as `/api/v1/slurmish`

### Requirement: production orchestration does not depend on QHH diagnostic scripts

Production scheduler/orchestrator code under `services/orchestrator` SHALL NOT call or import `scripts/run_qhh_continuous.py`, `scripts/run_qhh_cycle.sh`, or related diagnostic-only script tokens.

#### Scenario: production orchestration files are scanned

- **WHEN** static boundary tests scan production orchestrator modules
- **THEN** no QHH diagnostic script token appears in recursive production scheduler/orchestrator Python sources

### Requirement: Shared contract code does not depend upward on API modules

Shared contract packages, documented shared-contract Python files, workers, and orchestrator modules SHALL NOT import `apps.api` or `apps.api.*` for policy evidence or authorization helpers. Shared policy helpers required outside the API layer MUST live in a shared package, with API-only request
dependencies kept in `apps/api`.

#### Scenario: shared and worker imports are scanned

- **WHEN** static boundary tests scan `packages/common`, `services/orchestrator`, `workers/**`, and documented shared-contract Python files such as `services/slurm_gateway/models.py`
- **THEN** imports from `apps.api` or `apps.api.*` are absent, except for a temporary issue-scoped allowlist that points to the shared auth/policy extraction issue

#### Scenario: temporary API import allowlist is issue-scoped

- **WHEN** #360 static tests encounter the known `apps.api.auth` imports outside `apps/api`
- **THEN** only the exact paths documented for #361 are allowed, and any new `apps.api` or `apps.api.*` import outside the allowlist fails
- **AND** parent-package spellings such as `import apps.api`, `from apps.api import auth`, `from apps import api`, and wildcard imports are normalized before allowlist comparison

#### Scenario: shared policy helper extraction remains a follow-up

- **WHEN** #360 lands before #361
- **THEN** the static guard documents the exact temporary allowlist and does not require helper moves or call-site rewrites in the #360 PR
