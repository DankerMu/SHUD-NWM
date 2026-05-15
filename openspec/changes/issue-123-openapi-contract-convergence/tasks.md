## 1. Drift Inventory

- [x] 1.1 Review `tests/test_openapi_drift.py` `DEFERRED_ROUTES`; implement, remove from OpenAPI, or keep with issue-scoped comment for each remaining route.
- [x] 1.2 Review `INTERNAL_ROUTES`; document why each remains internal or promote the route to OpenAPI if it is public.
- [x] 1.3 Add/update tests so allowlist changes are intentional and future drift fails loudly.

## 2. Model Contract Alignment

- [x] 2.1 Align `GET /api/v1/models` OpenAPI schema with runtime envelope/page response.
- [x] 2.2 Align `active` query semantics in implementation and OpenAPI as `true|false|all`, preserving legacy behavior only where documented.
- [x] 2.3 Convert model registry errors to the project error envelope with stable error codes and HTTP statuses.
- [x] 2.4 Add model contract tests for list envelope/page shape, `active=true|false|all`, and error envelope.
- [x] 2.5 Add seeded model-list vectors: active and inactive fixtures must prove `active=true`, `active=false`, `active=all`, and omitted `active` return expected `items`, `total`, `limit`, and `offset`.
- [x] 2.6 Add error-envelope vectors for duplicate, missing resource, invalid reference, invalid payload, package validation, and generic registry errors, including HTTP status, `status="error"`, stable `error.code`, message/details shape, and `request_id`.

## 3. Flood Schema and Generated Types

- [x] 3.1 Fix flood timeline/threshold schemas so threshold maps generate meaningful object/value types instead of `Record<string, never>`.
- [x] 3.2 Regenerate `apps/frontend/src/api/types.ts` from OpenAPI.
- [x] 3.3 Add a regression test or type check that catches empty threshold object generation for key flood APIs.
- [x] 3.4 Assert generated flood `frequency_thresholds` types include concrete threshold properties such as `Q2/Q5/Q10/Q20/Q50/Q100` or numeric additional properties, and do not include `Record<string, never>`.

## 4. Required Evidence

- [x] 4.1 `openspec validate issue-123-openapi-contract-convergence --strict --no-interactive` passes.
- [x] 4.2 `uv run pytest -q tests/test_openapi_drift.py tests/test_api_contract.py tests/test_model_registration.py tests/test_flood_alerts_api.py` passes.
- [x] 4.3 `uv run pytest -q tests/test_api.py tests/test_gateway.py` passes.
- [x] 4.4 `uv run ruff check .` passes.
- [x] 4.5 `cd apps/frontend && corepack pnpm test` passes.
- [x] 4.6 `cd apps/frontend && corepack pnpm build` passes.

## Risk Pack Evidence Mapping

- Public API / CLI / script entry: tasks 1.1, 1.2, 2.1, 2.2, evidence 4.2.
- File IO / path safety / overwrite: tasks 3.2, 3.3, evidence 4.5.
- Schema / columns / units / field names: tasks 2.1, 2.5, 3.1, 3.2, 3.4.
- Time series / forcing / temporal boundaries: tasks 3.1, 3.3.
- Legacy compatibility / examples: tasks 1.1, 1.2, 2.2, 2.5.
- Error handling / rollback / partial outputs: tasks 2.3, 2.4, 2.6.
- Release / packaging / dependency compatibility: tasks 3.2, evidence 4.5, 4.6.
- Documentation / migration notes: tasks 1.1, 1.2.

## Non-Goals

- Frontend RBAC/data-source production migration from #125.
- Real database/e2e integration matrix from #126.
- Slurm Analysis/Hindcast path unification from #124.
