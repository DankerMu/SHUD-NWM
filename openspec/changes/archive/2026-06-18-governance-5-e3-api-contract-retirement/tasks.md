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

- [ ] 4.1 Update `openapi/nhms.v1.yaml` only after replacement and consumer migration evidence exists. (#416: removal gate not satisfied; no OpenAPI contraction is performed.)
- [ ] 4.2 Regenerate and verify frontend API types in the same implementation slice as OpenAPI contraction. (#416: not applicable because no OpenAPI contraction or generated type removal is performed.)
- [ ] 4.3 Update validation docs and API runbooks to reflect active vs deprecated route status. (#416: no runtime/API runbook status change; final explicit-deferral status is recorded in `docs/governance/API_CONTRACT_REMOVAL_DEFERRAL.md`.)
- [x] 4.4 For #415, confirm #413/#414 evidence records no backend/frontend removal-candidate migration and no replacement endpoint; preserve active OpenAPI paths and generated type entries unless drift is found. (#415: #413/#414 evidence records no migration and no replacement; OpenAPI/types preserved unchanged.)
- [x] 4.5 For #415, synchronize stale docs-only shorthand `forecast-series` references to the canonical basin-version route or mark them as historical draft examples without adding runtime deprecation metadata. (#415: canonical wording updated in module/frontend docs; appendix v0.2 snippet marked historical.)
- [x] 4.6 For #415, record an OpenAPI/generated-type synchronization evidence artifact that states whether OpenAPI/types changed, whether types were regenerated, and why no endpoint is deprecated, removed, or contracted. (#415: `docs/governance/API_CONTRACT_OPENAPI_TYPE_SYNC_EVIDENCE.md`.)

Expected #415 contract-sync outputs:

- `/api/v1/mvp/qhh/latest-product` remains present in static OpenAPI and generated frontend types as an active compatibility route.
- `/api/v1/basin-versions/{basin_version_id}/river-segments/{segment_id}/forecast-series` remains present in static OpenAPI and generated frontend types as the canonical active route.
- No OpenAPI `deprecated: true`, deprecation response header, response metadata, or generated deprecation marker is introduced for the #411 active runtime candidates.
- Docs-only shorthand `forecast-series` references are either replaced with the canonical route or explicitly labelled historical draft examples.
- #416 receives enough evidence to close as explicit deferral unless a later change produces real removal readiness.
- The evidence artifact records the rollback baseline: if docs/OpenAPI/type synchronization creates ambiguity, restore the active OpenAPI/generated type entries and canonical docs wording.

## 6. Removal Or Explicit Deferral Decision

- [x] 6.1 For #416, confirm #415 evidence proves removal-ready status before removing any endpoint implementation, OpenAPI path, generated type entry, or test coverage. (#416: gate is not satisfied; no endpoint implementation, OpenAPI path, generated type entry, or test coverage is removed.)
- [x] 6.2 For #416, if the removal gate is not satisfied, record explicit deferral for each active runtime candidate and preserve compatibility. (#416: `docs/governance/API_CONTRACT_REMOVAL_DEFERRAL.md` records explicit deferral for `latest-product` and canonical `forecast-series`; compatibility preserved.)
- [x] 6.3 For #416, record the final docs-only shorthand `forecast-series` disposition after #415: no runtime endpoint existed; stale docs were canonicalized or marked historical. (#416: closed as docs cleanup/historical retention; no runtime endpoint existed or was removed.)
- [x] 6.4 For #416, document what future evidence would be required to reopen removal for active runtime contracts. (#416: future evidence and rollback baseline recorded in `docs/governance/API_CONTRACT_REMOVAL_DEFERRAL.md`.)

Expected #416 outputs:

- `GET /api/v1/mvp/qhh/latest-product` is either removed only with repository no-consumer evidence, synchronized OpenAPI/generated types, passing compatibility checks, and external-consumer treatment/notice evidence, or explicitly deferred. Current #415 evidence points to explicit deferral.
- `GET /api/v1/basin-versions/{basin_version_id}/river-segments/{segment_id}/forecast-series` is either removed only with replacement evidence, repository no-consumer evidence, synchronized OpenAPI/generated types, passing compatibility checks, and external-consumer treatment/notice evidence, or explicitly deferred. Current #415 evidence points to explicit deferral.
- Docs-only shorthand `forecast-series` forms are closed as documentation cleanup/historical-retention, not runtime endpoint removal.
- No active endpoint is marked deprecated or removal-ready solely because repository docs were cleaned up.

Expected #416 search register:

- `rg -n '@router\.get\("(/mvp/qhh/latest-product|/basin-versions/\{basin_version_id\}/river-segments/\{segment_id\}/forecast-series)' apps/api/routes/forecast.py`
  -> both active route implementations remain.
- `rg -n '^  /api/v1/(mvp/qhh/latest-product|basin-versions/\{basin_version_id\}/river-segments/\{segment_id\}/forecast-series):|operationId: getQhhLatestProduct|operationId: getRiverSegmentForecastSeries' openapi/nhms.v1.yaml`
  -> both active static OpenAPI paths/operations remain.
- `rg -n '"/api/v1/(mvp/qhh/latest-product|basin-versions/\{basin_version_id\}/river-segments/\{segment_id\}/forecast-series)"|getQhhLatestProduct|getRiverSegmentForecastSeries' apps/frontend/src/api/types.ts`
  -> both generated type paths/operations remain.
- `rg -n -U "client\\.GET\\(\\s*['\"]/api/v1/(mvp/qhh/latest-product|basin-versions/\\{basin_version_id\\}/river-segments/\\{segment_id\\}/forecast-series)['\"]" apps/frontend/src`
  -> active frontend generated-client usage remains for both route families.
- `rg -n '/api/v1/mvp/qhh/latest-product|/api/v1/basin-versions/.*/forecast-series|/api/v1/basin-versions/\{basin_version_id\}/river-segments/\{segment_id\}/forecast-series' tests/test_api_contract.py tests/test_openapi_drift.py`
  -> active backend contract and OpenAPI drift coverage remains.
- `rg -n 'deprecated: true|Deprecation|deprecation header|X-Deprecated|Sunset' openapi/nhms.v1.yaml apps/frontend/src/api/types.ts docs/governance/API_CONTRACT_*.md`
  -> no active-candidate deprecation marker is introduced; governance docs may mention deprecation policy only as rationale.
- `git status --short --untracked-files=all`
  -> only deliberate #416 docs/OpenSpec/deferral evidence files change before staging; no API route implementation, OpenAPI path, generated frontend type, frontend runtime, or CI workflow files change.

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
- [x] 5.17 For #415, run repository searches covering static OpenAPI, generated frontend types, stale docs-only shorthand references, backend tests, and frontend generated-client usage for the #411 candidates. (#415: active OpenAPI/type paths retained; target docs-only shorthand cleaned or marked historical; backend tests and frontend generated-client usage remain active.)
- [x] 5.18 For #415, run `openspec validate governance-5-e3-api-contract-retirement --strict --no-interactive`. (#415: valid.)
- [x] 5.19 For #415, run `uv run --no-sync pytest -q tests/test_api_contract.py tests/test_openapi_drift.py`. (#415: 81 passed, 8 warnings.)
- [x] 5.20 For #415, run `cd apps/frontend && corepack pnpm run check:api-types`; also run `cd apps/frontend && corepack pnpm build` if OpenAPI or generated frontend types change. (#415: check:api-types green; build not required because OpenAPI/generated types did not change.)
- [x] 5.21 For #415, confirm `git status --short --untracked-files=all` is limited to docs/OpenSpec/OpenAPI/type-sync evidence and any deliberate static OpenAPI/generated type files; no API route implementation or frontend runtime files change. (#415: changed files are docs/governance evidence, docs wording, and E3 OpenSpec tasks/fixture evidence; no route implementation or frontend runtime file changed.)
- [x] 5.22 For #416, run repository searches covering active route implementations, static OpenAPI, generated frontend types, backend tests, frontend generated-client usage, and deprecation markers for the #411 candidates. (#416: active route/OpenAPI/type/frontend/test consumers remain; no OpenAPI/type deprecation marker found; governance docs contain policy rationale only.)
- [x] 5.23 For #416, run `openspec validate governance-5-e3-api-contract-retirement --strict --no-interactive`. (#416: valid.)
- [x] 5.24 For #416, run `uv run --no-sync pytest -q tests/test_api_contract.py tests/test_openapi_drift.py`. (#416: 81 passed, 8 warnings.)
- [x] 5.25 For #416, run `cd apps/frontend && corepack pnpm run check:api-types`. (#416: green; generated type diff check passed.)
- [x] 5.26 For #416, confirm `git status --short --untracked-files=all` is limited to docs/OpenSpec/deferral evidence and no API route implementation, OpenAPI path, generated frontend type, frontend runtime, or CI workflow files changed unless removal gates are explicitly satisfied. (#416: changed files are `docs/governance/API_CONTRACT_REMOVAL_DEFERRAL.md`, E3 `design.md`, and E3 `tasks.md`; no runtime/OpenAPI/type/frontend/CI files changed.)
