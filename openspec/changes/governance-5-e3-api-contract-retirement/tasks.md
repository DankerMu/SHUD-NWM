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

- [x] 2.1 For #412, define replacement endpoint or compatibility policy for each #411 candidate route.
- [x] 2.2 For #412, document migration order and rollback for current consumers.
- [x] 2.3 For #412, decide whether any deprecation warning, response metadata, or docs-only marker is appropriate before removal.
- [x] 2.4 For #412, map backend, node-27 frontend, OpenAPI/type/docs sync, and removal/defer follow-up responsibilities to #413/#414/#415/#416.

Expected #412 policy outputs:

- `/api/v1/mvp/qhh/latest-product` has an explicit compatibility/deprecation policy and is not marked deprecated or removal-ready in #412.
- Canonical `/api/v1/basin-versions/{basin_version_id}/river-segments/{segment_id}/forecast-series` has an explicit retain/defer policy and is not marked deprecated or removal-ready in #412.
- Docs-only shorthand `forecast-series` references have a docs-cleanup or historical-retention policy and do not imply a runtime deprecation header.
- Policy defines migration order: backend/test evidence if needed, node-27/frontend migration on node-27, OpenAPI/generated type/docs sync, then removal/defer decision.
- Policy defines rollback: keep active routes compatible until replacement consumers are proven and OpenAPI/type sync lands.
- Policy explicitly notes external consumers are unknown, so repository migration alone is not external deprecation proof.

## 3. Consumer Migration

- [x] 3.1 For #413, confirm backend/internal consumers of #411 removal-candidate endpoints, and migrate backend consumers if such consumers exist.
- [x] 3.2 On node-27, migrate frontend consumers if display bootstrap or display stores depend on a candidate compatibility endpoint. (#414: condition not met — #412 selected no replacement and marked no candidate removal-ready; all frontend consumers stay on the canonical active routes `/api/v1/mvp/qhh/latest-product` and `/api/v1/basin-versions/{basin_version_id}/river-segments/{segment_id}/forecast-series`. Explicit deferral; evidence in `docs/governance/API_CONTRACT_FRONTEND_CONSUMER_EVIDENCE.md`.)
- [x] 3.3 Keep old endpoint behavior available until all current consumers pass against replacement routes. (#414: no replacement exists; all active routes/types/consumers preserved unchanged.)
- [x] 3.4 For #413, if no backend removal-candidate consumers exist, record evidence and explicitly defer backend migration as not needed.

Expected #413 backend outputs:

- Backend/internal search covers `apps/api`, `packages/common`, `services`, `workers`, `tests`, and `scripts`.
- Active contract test coverage for `/api/v1/mvp/qhh/latest-product` and canonical `forecast-series` is preserved and not treated as dead code.
- Docs-only shorthand `forecast-series` references are not treated as backend runtime consumers unless a real route/code consumer is found.
- If no backend removal-candidate consumer exists, #413 closes with evidence rather than code migration.
- If backend code migration is required, focused backend tests are updated and API behavior stays backward compatible.

Expected #414 frontend outputs:

- Frontend/display search covers `apps/frontend/src` bootstrap, stores, generated client usage, and generated types.
- If no frontend consumer depends on a removal-candidate replacement (none exists under #412), #414 closes with evidence and explicit deferral rather than code migration.
- Generated types are not regenerated and OpenAPI is not contracted in #414; that remains #415 gated on real migration evidence.
- `cd apps/frontend && corepack pnpm run check:api-types && corepack pnpm build` stays green; no endpoint is deleted.

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
- [x] 5.7 For #412, run `openspec validate governance-5-e3-api-contract-retirement --strict --no-interactive`.
- [x] 5.8 For #412, run markdown lint over changed governance docs.
- [x] 5.9 For #412, confirm `git status --short --untracked-files=all` is limited to governance policy docs and E3 OpenSpec evidence.
- [x] 5.10 For #413, run backend/internal repository searches covering #411 candidate endpoints and shorthand forms.
- [x] 5.11 For #413, run `openspec validate governance-5-e3-api-contract-retirement --strict --no-interactive`.
- [x] 5.12 For #413, run backend tests if backend code changes; otherwise run markdown lint over changed governance docs and confirm no runtime code changed.
- [x] 5.13 For #413, confirm `git status --short --untracked-files=all` is limited to backend evidence/docs/OpenSpec and any deliberate backend/test files.
- [x] 5.14 For #414, run frontend/display repository searches covering generated client usage, stores/bootstrap consumers, and generated types for the #411 candidate endpoints and shorthand forms.
- [x] 5.15 For #414, run `cd apps/frontend && corepack pnpm run check:api-types && corepack pnpm build` (local + node-27 receipt) and confirm green with no endpoint deleted and no generated-type regeneration. (Local: check:api-types green, build ✓. node-27 receipt @e4d70eb: check:api-types green, build ✓ 16.57s.)
- [x] 5.16 For #414, confirm `git status --short --untracked-files=all` is limited to frontend consumer evidence/docs/OpenSpec (no `apps/frontend/src` runtime change). (e4d70eb --stat: only API_CONTRACT_FRONTEND_CONSUMER_EVIDENCE.md, issue-414-worklog.md, tasks.md.)
