## Context

The audit confirmed the role model is strong in runtime code: `ServiceRole` includes `dev_monolith`, `compute_control`, `display_readonly`, and `slurm_gateway`; Slurm routes are only mounted when `slurm_routes_enabled`; display retry/cancel and queue depth fail closed. The missing piece is durable
repository governance: an inventory plus static tests that keep future edits inside the role model.

The audit also found layer inversions. `packages/common`, `services/orchestrator`, and workers import `apps.api.auth`, which makes shared contracts depend on an API layer. That should not be fixed as an incidental cleanup; it needs a planned shared-policy extraction.

## Decisions

### D1. Use four categories, not two nodes

The governance model is:

| Category | Meaning |
|---|---|
| `compute_control` | node-22 control plane, scheduler, DB mutation, Slurm, SHUD runtime, publish. |
| `display_readonly` | node-27 display API/frontend, readonly DB and published artifact consumption. |
| `slurm_gateway` | bounded node-22 gateway exposing only Slurm gateway routes. |
| `shared_contract` | OpenAPI, DB migrations, common packages, schemas, generated types, run identity contracts. |

This avoids misclassifying shared contracts as either "22 code" or "27 code".

### D2. Enforce boundaries statically before moving code

The first PR should document and test existing boundaries. It should not physically split directories or perform a broad refactor.

### D3. Treat `apps.api.auth` layer inversion as a follow-up issue in this epic

Policy evidence helpers used by CLI/worker/common code should move into a shared package. API route dependencies should continue to live in `apps/api`.

The follow-up is not optional backlog. The Governance-1 epic should include a separate implementation-ready issue for shared auth/policy extraction after the inventory and static tests land.

## #360 OpenSpec Fixture

Fixture level: expanded

Repair intensity: high

Project profile: NHMS

Change surface:

- New role inventory: `docs/governance/ROLE_BOUNDARY.md`.
- New static guard suite: `tests/test_role_boundary_static.py`.
- Existing runtime/compose guard evidence: `tests/test_runtime_mode.py`, `tests/test_two_node_docker_runtime.py`, `tests/test_qhh_scripts_static.py`, `tests/test_retry_cancel_consistency.py`, and `tests/test_monitoring_api.py`.
- Reference implementations only: `apps/api/runtime_mode.py`, `apps/api/main.py`, `services/slurm_gateway/app.py`, `infra/env/*.example`, and `infra/compose.*.yml`.

Must preserve:

- `compute_control` and `dev_monolith` continue to expose Slurm routes through the full API.
- `display_readonly` continues to expose the existing non-Slurm business/display
  route inventory but not `/api/v1/slurm/*`. Some non-Slurm mutation-shaped
  model registry and hindcast routes remain a documented residual protected by
  auth checks and readonly DB/deployment posture rather than by the #360 runtime
  role guard.
- `display_readonly` continues to fail closed for retry/cancel (`409 CONTROL_PLANE_MANUAL_ACTION_REQUIRED`) and queue-depth (`503 CONTROL_PLANE_QUEUE_UNAVAILABLE`).
- The standalone Slurm gateway app continues to expose only `/api/v1/slurm` and `/api/v1/slurm/*` plus explicit framework docs/openapi surfaces, not forecast/model/pipeline/static/frontend routes or sibling prefixes such as `/api/v1/slurmish`.
- Existing QHH diagnostic scripts remain diagnostic-only and are not wired into production orchestrator code.
- Known `apps.api.auth` layer inversions remain only through the issue-scoped #361 temporary allowlist until #361 moves shared helpers into a shared package.

Must add/change:

- A four-role inventory that lets maintainers classify active paths as `compute_control`, `display_readonly`, `slurm_gateway`, or `shared_contract`.
- Static tests that fail on display compute-control leakage, standalone gateway business/frontend/static route leakage, QHH diagnostic token leakage into production orchestrator code, and new non-allowlisted `apps.api` / `apps.api.*` imports from shared/workers/orchestrator paths.
- A documented #361 temporary allowlist for the existing `apps.api.auth` imports outside `apps/api`.

Current temporary `apps.api.auth` allowlist for #361:

- (`packages/common/model_registry.py`, `apps.api.auth`)
- (`services/orchestrator/retry.py`, `apps.api.auth`)
- (`workers/flood_frequency/cli.py`, `apps.api.auth`)
- (`workers/flood_frequency/frequency.py`, `apps.api.auth`)
- (`workers/flood_frequency/hindcast.py`, `apps.api.auth`)
- (`workers/model_registry/basins_registry_import.py`, `apps.api.auth`)
- (`workers/model_registry/cli.py`, `apps.api.auth`)

Risk packs considered:

- Public API / CLI / script entry: selected - role guards affect API route registration and developer/operator scripts.
- Config / project setup: selected - display/compute env examples and compose role boundaries must stay separated.
- File IO / path safety / overwrite: not selected - #360 does not add runtime file access or write paths.
- Schema / columns / units / field names: not selected - no schema or generated API shape changes.
- Auth / permissions / secrets: selected - #360 documents and guards the `apps.api.auth` layer inversion allowlist without extracting helpers.
- Concurrency / shared state / ordering: not selected - no scheduler state-machine behavior changes.
- Resource limits / large input / discovery: not selected - no data discovery or large-input runtime behavior changes.
- Legacy compatibility / examples: selected - QHH diagnostic scripts and existing role env examples must remain classified without becoming production paths.
- Error handling / rollback / partial outputs: selected - display retry/cancel and queue-depth fail-closed behavior must remain documented and covered by existing runtime tests.
- Release / packaging / dependency compatibility: selected - static tests must run in the existing `uv run pytest` gate.
- Documentation / migration notes: selected - `ROLE_BOUNDARY.md` is the primary deliverable.
- Slurm production lifecycle / mock-vs-real parity: selected - Slurm route exposure must stay limited to compute/full API and bounded gateway.
- Run manifest / QC provenance: not selected - no run evidence contract change.
- Published NHMS artifacts / display identity: selected - display remains readonly consumer of DB/published artifacts.

Invariant Matrix

Governing invariant: runtime role determines allowed control-plane capability, and shared contracts must not depend upward on API-layer helpers except through the temporary #361 allowlist.

Source-of-truth identity/contract: `ServiceRole` values, route inventory, env key allow/deny lists, diagnostic script tokens, and #361 allowlisted import paths.

Surfaces:

- Producers: role/env examples in `infra/env/*.example`, route registration in `apps/api/main.py`, standalone gateway factory in `services/slurm_gateway/app.py`.
- Validators/preflight: `apps/api/runtime_mode.py`, `tests/test_runtime_mode.py`, `tests/test_two_node_docker_runtime.py`, `tests/test_role_boundary_static.py`.
- Storage/cache/query: none - #360 does not change DB/object-store persistence.
- Public routes/entrypoints: full API app, standalone Slurm gateway app including APIRoute and non-APIRoute route-like entries, Make/pytest validation entrypoints.
- Frontend/downstream consumers: `apps/frontend` as display consumer of readonly API; no frontend code changes in #360.
- Failure paths/rollback/stale state: display unsafe env startup failure, display retry/cancel 409, display queue-depth 503, route-scope static failures.
- Evidence/audit/readiness: `docs/governance/ROLE_BOUNDARY.md`, static tests, and focused runtime/two-node/QHH diagnostic tests.

Regression rows:

- `display_readonly` app with normal display env -> starts with readonly business routes and no `/api/v1/slurm/*`.
- `display_readonly` non-Slurm mutation-shaped route residual -> documented as
  follow-up Governance-1/Governance-4 scope, not claimed as removed by #360.
- `display_readonly` app with Slurm/compute-only env -> startup/static validation fails before serving unsafe control-plane capability.
- `display_readonly` retry/cancel requests -> `409 CONTROL_PLANE_MANUAL_ACTION_REQUIRED`, covered by `tests/test_retry_cancel_consistency.py`.
- `display_readonly` queue-depth request -> `503 CONTROL_PLANE_QUEUE_UNAVAILABLE`, covered by `tests/test_monitoring_api.py`.
- Standalone Slurm gateway app -> route inventory contains `/api/v1/slurm` or `/api/v1/slurm/*` and excludes forecast/model/pipeline/static/frontend business routes and sibling prefixes such as `/api/v1/slurmish`.
- Production orchestrator scan -> no `scripts/run_qhh_*` or `create_qhh_shud_manifest` diagnostic token appears in recursive production Python sources under `services/orchestrator`.
- Shared/workers/orchestrator import scan -> only the seven #361 allowlisted (`path`, `apps.api.auth`) pairs are present; any new `apps.api` / `apps.api.*` import fails.

Boundary-surface checklist:

- Shared helper roots: `packages/common`, `services/orchestrator`, `workers/**`, and documented shared-contract Python files such as `services/slurm_gateway/models.py`; #360 only scans and allowlists, #361 extracts.
- Public entrypoints: full API route inventory, standalone Slurm gateway route inventory.
- Read surfaces: display readonly DB/published artifact consumption documented in `ROLE_BOUNDARY.md`.
- Write/delete/overwrite surfaces: compute-control only; no new writes in #360.
- Producer/consumer evidence boundaries: node-22 writes shared contracts, node-27 reads shared contracts.
- Unchanged downstream consumers: frontend build/runtime code is unchanged; existing runtime tests remain compatibility evidence.

## Boundary Tests

Static and focused runtime tests should assert:

- `display_readonly` does not register `/api/v1/slurm/*`.
- `display_readonly` blocks `SLURM_GATEWAY_URL`, `SLURM_GATEWAY_BACKEND`, `WORKSPACE_ROOT`, `SHUD_EXECUTABLE`, Docker/socket/control mutations, and compute-only path env.
- retry/cancel return `409 CONTROL_PLANE_MANUAL_ACTION_REQUIRED` under display (`tests/test_retry_cancel_consistency.py` is the compatibility evidence).
- queue depth returns `503 CONTROL_PLANE_QUEUE_UNAVAILABLE` under display (`tests/test_monitoring_api.py` is the compatibility evidence).
- current display non-Slurm mutation-shaped routes are explicitly called out as
  residual follow-up scope; #360 does not edit runtime route registration for
  model registry or hindcast routes.
- `services/slurm_gateway/app.py` exposes Slurm gateway routes only, uses slash-delimited `/api/v1/slurm` matching, and does not include forecast/model/pipeline/static/frontend routes or mounts.
- recursive production Python sources under `services/orchestrator` do not reference `scripts/run_qhh_*` diagnostic tokens.
- `packages/common`, `services/orchestrator`, `workers/**`, and documented shared-contract Python files such as `services/slurm_gateway/models.py` do not import `apps.api` / `apps.api.*` except through a documented temporary allowlist while the shared-policy extraction issue is open.

## Risks / Mitigations

- **Risk: tests overfit strings.** Mitigation: static tests should check stable route/env/role invariants and be paired with existing runtime tests.
- **Risk: shared-policy extraction is too broad.** Mitigation: split it as a separate sub-issue after the inventory lands.
- **Risk: docs become stale.** Mitigation: Governance-4 later adds audit automation for boundary inventory drift.

## Verification

- `uv run pytest -q tests/test_runtime_mode.py tests/test_two_node_docker_runtime.py`
- `uv run pytest -q tests/test_qhh_scripts_static.py tests/test_role_boundary_static.py`
- `uv run ruff check .`
