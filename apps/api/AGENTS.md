# API Agent Instructions

This file scopes root `AGENTS.md` for `apps/api/`. The current authority for
shared governance vocabulary is `openspec/glossary.md`; reuse terms such as
active entrypoint, current authority, budget-counted finding, and gate-eligible
finding exactly as the glossary defines them.

## Required Reading

- `openspec/changes/governance-7-structural-entropy-controls/specs/scoped-agent-context-governance/spec.md`
- `docs/governance/ROLE_BOUNDARY.md`
- `docs/runbooks/qhh-backend-smoke.md`
- `openspec/glossary.md`

`docs/runbooks/qhh-backend-smoke.md` is a runbook freshness anchor for the QHH
backend smoke chain, but much of it is historical evidence for a recorded local
diagnostic run. Do not use it as proof of current production readiness, current
node-27 live display receipt, or current role-boundary closure unless the active
issue/runbook explicitly says that live evidence was produced.

## Bootstrap And Routing Boundaries

- `apps/api/main.py:create_app()` is the active entrypoint for the full business
  API. Keep router inclusion, middleware registration, OpenAPI patching, static
  frontend serving, health routes, and display cache warmup coordinated there.
- `apps/api/runtime_mode.py` owns `ServiceRole`, `RuntimeConfig`, service-role
  startup validation, display boundary blockers, and public runtime capability
  flags. Do not duplicate role parsing or control-plane capability decisions in
  route modules.
- Slurm routes are included in the full API only when
  `runtime_config.slurm_routes_enabled` is true. `display_readonly` must not
  register `/api/v1/slurm/*`; `slurm_gateway` must use the standalone gateway
  app, not the full business API.
- Route modules under `apps/api/routes/` own request/response behavior for their
  domain. Shared contracts, auth policy, redaction, forecast stores, and
  persistence helpers should stay in `packages/`, `services/`, or `workers/`
  owner modules rather than being copied into API routes.

## Runtime Role Guards

- Keep `display_readonly`, `compute_control`, and `dev_monolith` semantics aligned
  with `docs/governance/ROLE_BOUNDARY.md`. Local checks do not replace node-27
  live DB/display receipts where the runbook requires them.
- `display_readonly` may serve readonly responses, runtime config, static
  assets, MVT/display data, and fail-closed manual-action responses. It must not
  gain Slurm submission/cancel/retry capability, compute roots, writer database
  posture, or control mutations by configuration.
- `compute_control` may expose control-plane capability through the full API
  only under the configured role contract. Node-22 is currently the compute/Slurm
  oracle and must not be treated as the active database writer.
- Protected mutation routes must flow through the existing auth policy helpers
  and middleware in `apps/api/main.py` / `apps/api/auth.py`; do not add a route
  that mutates model, pipeline, hindcast, Slurm, or control state without an
  explicit policy action and role-boundary test.

## Error Model

- API errors should use `apps/api/errors.py` (`ApiError`, `error_response`, and
  registered handlers) so responses keep `request_id`, `status`, `error.code`,
  `error.message`, and `error.details` stable.
- Request validation errors should preserve the shared `VALIDATION_ERROR` shape;
  Slurm request validation remains routed to the Slurm gateway validation error
  adapter.
- Fail-closed role-boundary behavior should return stable domain codes such as
  display readonly queue/manual-action blockers instead of generic 500s,
  swallowed errors, or route-local ad hoc response shapes.

## Focused Verification

Always run the issue-required scoped-context checks after changing this file or
API scoped context:

```bash
uv run pytest -q tests/test_entropy_audit_script.py tests/test_runtime_mode.py tests/test_api.py
openspec validate --all --strict --no-interactive
```

For route, runtime-role, or auth-boundary changes, add the relevant focused tests,
commonly:

```bash
uv run pytest -q tests/test_role_boundary_static.py tests/test_retry_cancel_consistency.py tests/test_monitoring_api.py
```
