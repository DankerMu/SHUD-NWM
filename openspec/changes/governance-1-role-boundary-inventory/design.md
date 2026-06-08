## Context

The audit confirmed the role model is strong in runtime code: `ServiceRole` includes `dev_monolith`, `compute_control`, `display_readonly`, and `slurm_gateway`; Slurm routes are only mounted when `slurm_routes_enabled`; display retry/cancel and queue depth fail closed. The missing piece is durable repository governance: an inventory plus static tests that keep future edits inside the role model.

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

## Boundary Tests

Static and focused runtime tests should assert:

- `display_readonly` does not register `/api/v1/slurm/*`.
- `display_readonly` blocks `SLURM_GATEWAY_URL`, `SLURM_GATEWAY_BACKEND`, `WORKSPACE_ROOT`, `SHUD_EXECUTABLE`, Docker/socket/control mutations, and compute-only path env.
- retry/cancel return `409 CONTROL_PLANE_MANUAL_ACTION_REQUIRED` under display.
- queue depth returns `503 CONTROL_PLANE_QUEUE_UNAVAILABLE` under display.
- `services/slurm_gateway/app.py` exposes Slurm gateway routes only and does not include forecast/model/pipeline/static/frontend routes.
- production orchestrator files do not reference `scripts/run_qhh_*` diagnostic tokens.
- `packages/common`, `services/orchestrator`, and `workers/**` do not import `apps.api.*` except through a documented temporary allowlist while the shared-policy extraction issue is open.

## Risks / Mitigations

- **Risk: tests overfit strings.** Mitigation: static tests should check stable route/env/role invariants and be paired with existing runtime tests.
- **Risk: shared-policy extraction is too broad.** Mitigation: split it as a separate sub-issue after the inventory lands.
- **Risk: docs become stale.** Mitigation: Governance-4 later adds audit automation for boundary inventory drift.

## Verification

- `uv run pytest -q tests/test_runtime_mode.py tests/test_two_node_docker_runtime.py`
- `uv run pytest -q tests/test_qhh_scripts_static.py tests/test_role_boundary_static.py`
- `uv run ruff check .`
