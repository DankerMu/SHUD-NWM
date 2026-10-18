# Slurm Gateway Agent Instructions

This file is the scoped supplement of root `AGENTS.md` for
`services/slurm_gateway/`. Shared vocabulary lives in `openspec/glossary.md`;
use its terms exactly as defined there.

## Required Reading

- `docs/runbooks/slurm-gateway-preflight.md`
- `openspec/specs/real-slurm-gateway-contract/spec.md`
- `openspec/specs/slurm-gateway-node22-deployment/spec.md`
- `docs/governance/ROLE_BOUNDARY.md`

The `slurm_gateway` section of `docs/governance/ROLE_BOUNDARY.md` is the
authority for what this service may and may not do.

## Ownership Boundaries

- `app.py:create_gateway_app()` builds the standalone gateway. It mounts only
  the Slurm router (`/api/v1/slurm` and `/api/v1/slurm/*`) and never business,
  static, or frontend routes; the full business API stays in `apps.api.main`.
  The audit scans `app.py` for business-route tokens as a hard gate.
- `/api/v1/slurm/internal/reset` is registered only when
  `SLURM_GATEWAY_ALLOW_INTERNAL_RESET` is enabled. When disabled it is absent
  from the route inventory (404), not a 403.
- `__main__.py` serves only the bounded app and fails closed before uvicorn
  starts when the bind host is not loopback (`127.0.0.0/8` or `::1`).
- `gateway.py` owns the `SlurmGateway` interface, the error hierarchy, and
  `create_gateway()`, which picks `mock_backend.py` or `real_backend.py` from
  `config.py:SlurmGatewaySettings.backend` (env prefix `SLURM_GATEWAY_`).
  `routes.py` owns HTTP translation and the mutation auth decision.
- `models.py` is a shared contract (request/response models). The canonical
  sbatch templates live in `infra/sbatch/`; `real_backend.py` renders them in a
  sandboxed Jinja environment with `StrictUndefined`. Resource profiles come
  from `config/resource_profiles.yaml` through `resource_profiles.py`, which
  the scheduler also uses; keep one loader with one set of fail-closed rules.
- The gateway never mutates NHMS business database rows or published artifact
  state, and never imports `apps`. Callers such as `apps/api/routes/pipeline.py`
  and `services/orchestrator/retry.py` consume `SlurmGatewayError`; changing a
  code or status changes their behavior too.

## Error Model

- Every backend failure is a `SlurmGatewayError(status_code, code, message,
  details)` from `gateway.py`. Subclasses fix the pair: `SlurmParseError` 502
  `SLURM_PARSE_ERROR`, `SlurmCommandError` 502 `SLURM_COMMAND_ERROR`,
  `SlurmTimeoutError` 504 `SLURM_TIMEOUT`, `SlurmJobNotFoundError` 404
  `JOB_NOT_FOUND`, `TemplateSecurityError` 400, `TemplateNotFoundError` 404,
  `ManifestValidationError` 422, `ConfigurationError` 500, and
  `SlurmValidationError` 422 `VALIDATION_ERROR`. Add a subclass rather than a
  new ad hoc `status_code`/`code` pair at a raise site.
- `routes.py` turns `SlurmGatewayError` into the `ErrorResponse` envelope
  (`request_id`, `status`, `error.code`, `error.message`, `error.details`).
- `validation_errors.py:slurm_request_validation_error_response()` is the only
  request-validation shape: 422 `VALIDATION_ERROR` with
  `details.validation_errors` entries of `field`, `reason`, and `type`. Field
  path parts that look like secret manifest keys are replaced by `[redacted]`,
  and raw input values are never echoed. The full API reuses this function from
  `apps/api/errors.py` for Slurm routes, so both apps stay identical.
- Mutation auth denials come from `routes.py:_policy_error_response()`: 401
  `AUTH_REQUIRED`, 403 `RBAC_FORBIDDEN` or `POLICY_CONFIG_ERROR`, 503
  `RELEASE_BLOCKED`, each with a redacted policy decision and audit record.
- Command output placed in `details` goes through `redact_text` and the bounded
  snippet helpers in `real_backend.py`; never return raw stderr or environment.

## Focused Verification

Always run the issue-required governance checks after changing this file or
gateway scoped context:

```bash
uv run pytest -q tests/test_entropy_audit_*.py
openspec validate --all --strict --no-interactive
```

For app, route, error, backend, or reconcile changes, run the focused set:

```bash
uv run pytest -q tests/test_gateway.py tests/test_slurm_gateway_app.py tests/test_slurm_route_contract.py tests/test_slurm_route_security_contract.py tests/test_gateway_reconcile_*.py
```
