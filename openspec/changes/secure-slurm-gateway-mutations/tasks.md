Fixture level: expanded
Repair intensity: high
Project profile: NHMS
Upstream suggested level: absent

## 1. Shared Authentication and RBAC

- [x] 1.1 Add canonical `slurm.submit_job`, `slurm.cancel_job`, and `slurm.reset_registry` role mappings in the shared policy owner.
- [x] 1.2 Extract/reuse request-auth context construction below the API layer and preserve the `apps.api.auth` facade and existing dev/live behavior.
- [x] 1.3 Add route-scoped, constant-time service bearer authentication and secret-safe policy/audit evidence.

## 2. Slurm Mutation Enforcement and Client Compatibility

- [x] 2.1 Attach one pre-handler auth dependency to single submit, array submit, cancel, and conditionally registered reset on both router mounts.
- [x] 2.2 Prove anonymous, wrong-token, wrong-role, release-blocked, and scheduler-token reset requests create no gateway/Slurm/registry side effect.
- [x] 2.3 Forward the service token only on HTTP client mutation calls and preserve submit/cancel response/error compatibility.
- [x] 2.4 Preserve standalone reset 404 when disabled and the `display_readonly` no-Slurm-route boundary.

## 3. Contract and Deployment

- [x] 3.1 Publish a token-free Slurm service bearer scheme and two exact security sets: the original 11 business mutations keep only the existing three alternatives; the four Slurm mutations add the service bearer; public Slurm reads stay root-anonymous.
- [x] 3.2 Make the documented protected-operation set equal the enforced set across every HTTP method, including DELETE, and keep runtime OpenAPI, static YAML, and generated API types equal and secret-free.
- [x] 3.3 Update node-22 env/systemd examples and `docs/runbooks/current-production-ops.md` with owner-only credential rollout, fail-closed loopback bind guard, rollback, remote-refusal receipt commands, and optional packet-filter evidence only when actually installed.
- [ ] 3.4 Produce a node-22 live receipt proving local health, anonymous/wrong-token denial, valid-token pre-validation passage, reset 404, loopback binding on the measured 8090 port, deliberate non-loopback-start rejection, remote refusal, and resumed scheduler health.

## 4. Required Evidence

- [x] 4.1 For each of submit, array submit, cancel, and enabled reset: no credential or a wrong bearer -> `401 AUTH_REQUIRED`; a valid viewer identity -> `403 RBAC_FORBIDDEN`; no gateway construction/call, `sbatch`, `scancel`, or registry mutation.
- [x] 4.2 Scheduler service bearer + enabled reset -> `403 RBAC_FORBIDDEN`; live mode without accepted proof -> `503 RELEASE_BLOCKED`; both paths leave registry/Slurm state unchanged.
- [x] 4.3 Disabled standalone reset + no, wrong, or valid credential -> route remains unregistered and returns `404`, never `401`/`403`.
- [x] 4.4 Valid scheduler bearer + invalid submit/array body -> auth passes and existing validation error is returned with zero `sbatch`/registry side effect; valid bearer + valid submit/cancel -> existing HTTP client success/error shape is preserved.
- [x] 4.5 Scheduler service bearer presented to an original business mutation such as `POST /api/v1/runs/{run_id}/retry` -> `401` or `403` according to existing auth semantics and no business/gateway side effect.
- [x] 4.6 Runtime/static OpenAPI test derives all enforced methods including DELETE -> original 11 operations have exactly three alternatives, four Slurm mutations have those three plus `SlurmServiceBearer`, public reads have no override, and token value is absent; generated types are current.
- [ ] 4.7 Node-22 `ss -ltnp` -> only loopback at measured port 8090; `python -m services.slurm_gateway --url http://0.0.0.0:8090` -> nonzero before uvicorn; remote host TCP/8090 probe -> connection refused/timed out; local health and credentialed pre-validation -> reachable; resumed scheduler preflight -> healthy; packet-filter evidence is recorded only if installed.
- [x] 4.8 `uv run pytest -q tests/test_auth_policy_matrix.py tests/test_slurm_gateway_auth.py tests/test_slurm_gateway_auth_fullmount.py tests/test_slurm_gateway_auth_client.py tests/test_slurm_gateway_auth_deployment.py tests/test_slurm_gateway_deployment_contract.py tests/test_slurm_gateway_app.py tests/test_slurm_gateway_openapi_security.py tests/test_gateway.py tests/test_slurm_route_contract.py tests/test_slurm_route_security_contract.py tests/test_orchestration_chain.py tests/test_openapi_31_contract.py tests/test_openapi_drift.py tests/test_runtime_mode.py tests/test_role_boundary_static.py tests/test_select_ci_tests.py` and `bash .claude/hooks/large-file-guard/test-large-file-guard.sh` pass on the final head.
- [x] 4.9 `uv run ruff check packages/common apps/api services/slurm_gateway services/orchestrator tests/test_slurm_gateway_auth.py tests/test_auth_policy_matrix.py tests/test_openapi_31_contract.py` passes.
- [x] 4.10 `openspec validate secure-slurm-gateway-mutations --strict --no-interactive`, frontend API-type drift check, and `git diff --check` pass.

## Risk Packs Considered

- Public API / CLI / script entry: selected - dual-mounted HTTP router, client, and service entrypoint change.
- Config / project setup: selected - new secret env and unit/env wiring.
- File IO / path safety / overwrite: not selected - no file path or publish/delete behavior changes.
- Schema / columns / units / field names: selected - OpenAPI security scheme/operation metadata changes.
- Auth / permissions / secrets: selected - governing risk; require fail-closed auth, RBAC, constant-time comparison, and redaction.
- Concurrency / shared state / ordering: selected - authorization must precede gateway calls and reset of shared registry state.
- Resource limits / large input / discovery: not selected - auth runs before existing bounded request validation; no new discovery/read surface.
- Legacy compatibility / examples: selected - existing scheduler client, API auth facade, mock tests, and deployment templates must remain usable.
- Error handling / rollback / partial outputs: selected - stable 401/403/503/404 and no side effects; staged rollout/rollback.
- Release / packaging / dependency compatibility: selected - no new dependency; deployment order and generated types are release-sensitive.
- Documentation / migration notes: selected - node-22 credential, loopback bind-guard, remote-refusal, rollout/rollback runbook and receipt required.
- Geospatial / CRS / basin geometry: not selected - untouched.
- Hydro-met time series / forcing windows: not selected - untouched.
- SHUD numerical runtime / conservation / NaN: not selected - no solver behavior change.
- PostGIS / TimescaleDB domain behavior: not selected - no DB access or migration.
- Slurm production lifecycle / mock-vs-real parity: selected - submit/cancel client continuity and live node-22 proof.
- External hydro-met providers / snapshot reproducibility: not selected - untouched.
- Run manifest / QC provenance: not selected - payload and evidence identity are unchanged.
- Published NHMS artifacts / display identity: not selected - display remains route-free and no publication behavior changes.

## Invariant Matrix

- Governing invariant: every registered Slurm mutation has exactly one allowed canonical policy decision before any gateway side effect, while legitimate scheduler calls and disabled-reset 404 remain intact.
- Source-of-truth identity/contract: `ACTION_MATRIX` action id plus route-scoped auth context derived from `SLURM_GATEWAY_SERVICE_TOKEN` or an existing allowed identity mode.
- Producers: `HttpSlurmGatewayClient` mutation headers and existing API identity headers.
- Validators/preflight: shared request-auth resolver, Slurm route dependency, scheduler gateway preflight.
- Storage/cache/query: gateway-local registry only; no DB/schema changes.
- Public routes/entrypoints: full compute/dev API router and standalone `create_gateway_app`; four mutation operations.
- Frontend/downstream consumers: static OpenAPI/generated types and existing orchestrator/scheduler client.
- Failure paths/rollback/stale state: 401/403/503 before calls, disabled reset 404, staged node-22 timer stop/restart/rollback.
- Evidence/audit/readiness: request policy records, focused tests, all-method OpenAPI enforcement parity, service-token business-route rejection, node-22 live receipt.
- Regression rows:
  - Valid scheduler token + submit/cancel boundary -> policy allows and existing client contract continues.
  - Missing/wrong bearer + each registered mutation -> `401 AUTH_REQUIRED`; viewer or scheduler-reset -> `403 RBAC_FORBIDDEN`; release-blocked identity -> `503`; all have zero gateway calls/side effects.
  - Scheduler token + original business mutation -> rejected with no side effect; OpenAPI retains separate business and Slurm security sets including DELETE parity.
  - Disabled standalone reset + any credential -> 404; display role -> no Slurm routes; read/health -> unchanged anonymous behavior.

## Boundary-Surface Checklist

- Shared helper roots: `packages/common/auth_policy.py` and reusable request-auth owner.
- Public entrypoints: `apps.api.main:create_app`, `services.slurm_gateway.app:create_gateway_app`, and module CLI.
- Read surfaces: health/list/status/results/log routes stay unchanged and token-free.
- Write/delete/overwrite surfaces: submit, array submit, cancel, reset all guarded.
- Staging/publish/rollback surfaces: node-22 unit/env rollout and rollback only; no artifact publication changes.
- Producer/consumer evidence boundaries: HTTP client header -> route auth context -> policy/audit decision -> gateway call.
- Stale-state/idempotency boundaries: denied calls cannot reserve, submit, cancel, or clear registry state.
- Unchanged downstream consumers: display role, existing API auth callers, mock gateway tests, scheduler response/error handling.

## Non-Goals

- Auth for Slurm reads, production IdP implementation, new roles, reset enablement, database work, or public gateway exposure.
