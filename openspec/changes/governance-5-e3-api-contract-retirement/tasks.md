## 1. Contract Inventory

- [x] 1.1 For #411, inventory legacy-looking forecast/latest-product API routes and classify each as active, deprecated, replacement-ready, or removal-ready.
- [x] 1.2 For #411, identify all repository consumers: API route tests, frontend stores/bootstrap, generated types, OpenAPI docs, and runbooks.
- [x] 1.3 For #411, explicitly mark `/api/v1/mvp/qhh/latest-product` or equivalent compatibility routes as active until consumers are migrated.
- [x] 1.4 For #411, record each candidate endpoint in a governance inventory artifact with route owner, OpenAPI status, generated type status, backend/test consumers, frontend consumer evidence, docs/runbook references, follow-up issue, and removal readiness.
- [x] 1.5 For #411, verify that frontend findings are evidence-only and no node-27/frontend implementation files are changed.

Expected #411 inventory outputs:

- Candidate endpoint matrix includes `/api/v1/mvp/qhh/latest-product` and any other legacy-looking forecast/latest-product compatibility route found in route definitions, OpenAPI, generated types, tests, frontend consumers, or docs.
- Each candidate has one of these statuses: `active`, `deprecated`, `replacement-ready`, `removal-ready`.
- No candidate is marked `removal-ready` while backend tests, OpenAPI/generated types, frontend display/bootstrap code, E2E specs, or docs/runbooks still consume it.
- `/api/v1/mvp/qhh/latest-product` is classified as active compatibility unless consumer evidence proves otherwise.
- Backend follow-up work is mapped to #413 when repository backend consumers require migration.
- Node-27/frontend follow-up work is mapped to #414 when frontend display/bootstrap consumers require migration.
- OpenAPI/type synchronization is deferred to #415 until #413/#414 migration evidence exists.
- Endpoint removal or explicit deferral is deferred to #416 after #415.

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

- [x] 5.1 Run `openspec validate governance-5-e3-api-contract-retirement --strict --no-interactive`.
- [ ] 5.2 Run `uv run --no-sync pytest -q tests/test_api_contract.py tests/test_openapi_drift.py` or the repository's current equivalent API/OpenAPI contract test set for any implementation PR.
- [ ] 5.3 Run `cd apps/frontend && corepack pnpm run check:api-types && corepack pnpm build` for any node-27 consumer migration or OpenAPI contraction.
- [ ] 5.4 Prove no current consumer still calls a route before removing it from OpenAPI.
- [x] 5.5 For #411, run repository searches covering route definitions, OpenAPI, generated frontend types, backend tests, frontend consumers, E2E/mocked specs, and docs/runbooks.
- [x] 5.6 For #411, confirm `git status --short --untracked-files=all` is limited to governance inventory/docs and E3 OpenSpec evidence.
