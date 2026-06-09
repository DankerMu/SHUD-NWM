## 1. Contract Inventory

- [ ] 1.1 Inventory legacy-looking forecast/latest-product API routes and classify each as active, deprecated, replacement-ready, or removal-ready.
- [ ] 1.2 Identify all repository consumers: API route tests, frontend stores/bootstrap, generated types, OpenAPI docs, and runbooks.
- [ ] 1.3 Explicitly mark `/api/v1/mvp/qhh/latest-product` or equivalent compatibility routes as active until consumers are migrated.

## 2. Replacement And Deprecation Plan

- [ ] 2.1 Define replacement endpoint or compatibility policy for each candidate route.
- [ ] 2.2 Document migration order and rollback for current consumers.
- [ ] 2.3 Decide whether any deprecation warning, response metadata, or docs-only marker is appropriate before removal.

## 3. Consumer Migration

- [ ] 3.1 Migrate backend consumers if inventory finds backend code depending on a candidate compatibility endpoint.
- [ ] 3.2 On node-27, migrate frontend consumers if display bootstrap or display stores depend on a candidate compatibility endpoint.
- [ ] 3.3 Keep old endpoint behavior available until all current consumers pass against replacement routes.

## 4. Contract Update

- [ ] 4.1 Update `openapi/nhms.v1.yaml` only after replacement and consumer migration evidence exists.
- [ ] 4.2 Regenerate and verify frontend API types in the same implementation slice as OpenAPI contraction.
- [ ] 4.3 Update validation docs and API runbooks to reflect active vs deprecated route status.

## 5. Verification

- [ ] 5.1 Run `openspec validate governance-5-e3-api-contract-retirement --strict --no-interactive`.
- [ ] 5.2 Run `uv run --no-sync pytest -q tests/test_api_contract.py tests/test_openapi_drift.py` or the repository's current equivalent API/OpenAPI contract test set for any implementation PR.
- [ ] 5.3 Run `cd apps/frontend && corepack pnpm run check:api-types && corepack pnpm build` for any node-27 consumer migration or OpenAPI contraction.
- [ ] 5.4 Prove no current consumer still calls a route before removing it from OpenAPI.
