# API Contract Retirement Inventory

Generated: 2026-06-11

Scope: Governance-5 E3 issue #411 repository inventory for legacy-looking
forecast/latest-product API contracts. This is evidence only. It does not
change route behavior, OpenAPI, generated frontend types, frontend
implementation, tests, CI, or runtime code.

## Status Vocabulary

Status values are limited to the E3 vocabulary:

- `active`: current runtime/OpenAPI/generated contract surface with repository
  consumers.
- `deprecated`: explicitly marked as no longer preferred while still available.
- `replacement-ready`: a replacement contract exists, but remaining references
  still need documentation or consumer cleanup before removal can be claimed.
- `removal-ready`: no current route, OpenAPI/generated type, test, frontend,
  E2E, docs, or runbook consumer remains.

Owner roles use the governed role vocabulary from
`docs/governance/ROLE_BOUNDARY.md`: `compute_control`, `display_readonly`,
`slurm_gateway`, and `shared_contract`.

## Conclusions

- `/api/v1/mvp/qhh/latest-product` is an active compatibility contract, not dead
  code. It remains used by route definitions, OpenAPI, generated frontend
  types, backend tests, frontend display/bootstrap code, and runbooks.
- `/api/v1/mvp/qhh/latest-product` is not `removal-ready` while those repository
  consumers remain. It must stay compatible until #413/#414 migrate consumers,
  #415 synchronizes OpenAPI/generated types, and #416 makes the removal or
  deferral decision.
- The broad repository search found no second runtime `latest-product` route.
- The active river `forecast-series` route is the canonical current forecast
  series API, not dead code. It remains consumed by backend tests, generated
  types, frontend stores, hydro-met helpers, mocked E2E, and runbooks.
- Several docs contain old shorthand `forecast-series` paths that are not
  route/OpenAPI/generated contracts. Those docs-only references are
  `replacement-ready` because the canonical route exists, but they are not
  `removal-ready` as documentation references still need cleanup or explicit
  deferral.

## Search Command Register

`C1` route and store inventory:

```bash
rg -n '@router\.(get|post|put|delete)\(".*(forecast|latest|mvp)|operation_id="getQhhLatestProduct"|latest_qhh|QHH_LATEST' apps/api/routes apps/api/main.py packages/common/forecast_store.py
```

`C2` OpenAPI inventory:

```bash
rg -n '^  /api/v1/.*(forecast|latest|mvp)|operationId: getQhhLatestProduct|QhhLatestProduct|forecast-series' openapi/nhms.v1.yaml
```

`C3` generated frontend type inventory:

```bash
rg -n '"/api/v1/.*(forecast|latest|mvp)|getQhhLatestProduct|QhhLatestProduct|forecast-series' apps/frontend/src/api/types.ts
```

`C4` latest-product consumer and docs inventory:

```bash
rg -n '/api/v1/mvp/qhh/latest-product|mvp/qhh/latest-product|latest-product' tests apps/api packages/common openapi/nhms.v1.yaml apps/frontend/src apps/frontend/e2e docs/runbooks docs/plans docs/spec docs/modules docs/appendices docs/governance || true
```

`C5` forecast-series consumer and docs inventory:

```bash
rg -n '/api/v1/basin-versions/\{basin_version_id\}/river-segments/\{segment_id\}/forecast-series|/api/v1/river-segments/\{segment_id\}/forecast-series|/api/v1/river-segments/\{id\}/forecast-series|/river-segments/\{segment_id\}/forecast-series|forecast-series' tests apps/api packages/common openapi/nhms.v1.yaml apps/frontend/src apps/frontend/e2e docs/runbooks docs/plans docs/spec docs/modules docs/appendices docs/governance || true
```

`C6` frontend and mocked E2E API consumption inventory:

```bash
rg -n 'page\.route\(|client\.GET\(|fetchHydroMetLatestProduct|loadHydroMetBootstrap|latest-product|forecast-series' apps/frontend/src apps/frontend/e2e || true
```

`C7` current E2E file inventory:

```bash
rg -n 'latest-product|mvp/qhh|forecast-series|page\.route\(' apps/frontend/e2e || true
rg --files apps/frontend/e2e | sort
```

## Candidate Matrix

| Candidate endpoint | Status | Route owner | Owner role | Follow-up issue | Removal readiness |
|---|---|---|---|---|---|
| `GET /api/v1/mvp/qhh/latest-product` | `active` | `apps/api/routes/forecast.py:get_qhh_latest_product`; store implementation in `packages/common/forecast_store.py` | `display_readonly` runtime read surface; `shared_contract` for OpenAPI/generated types | #413 backend/test migration if a replacement is chosen; #414 frontend/node-27 migration; #415 OpenAPI/type sync; #416 removal or explicit deferral | Not removal-ready. Current route, OpenAPI, generated types, backend tests, frontend display/bootstrap consumers, and runbooks remain. |
| `GET /api/v1/basin-versions/{basin_version_id}/river-segments/{segment_id}/forecast-series` | `active` | `apps/api/routes/forecast.py:get_forecast_series`; store implementation in `packages/common/forecast_store.py` | `display_readonly` runtime read surface; `shared_contract` for OpenAPI/generated types | #413/#414 only if a later issue chooses to migrate forecast-series consumers; #415/#416 must defer contraction while consumers remain | Not removal-ready. It is the canonical active forecast-series API with current backend/frontend/docs consumers. |
| Docs-only shorthand family: `GET /api/v1/river-segments/{segment_id}/forecast-series`, `GET /api/v1/river-segments/{id}/forecast-series`, and relative `/river-segments/{segment_id}/forecast-series` | `replacement-ready` | No current route definition; references are in docs only | `shared_contract` documentation cleanup | #415 docs/API contract synchronization; #416 explicit removal or deferral of stale documentation references | Not runtime removal-ready because no runtime endpoint exists; not docs-removal-ready until stale docs references are updated or explicitly retained as historical. |

## Consumer Evidence

### `GET /api/v1/mvp/qhh/latest-product`

- Route definition: `apps/api/routes/forecast.py:115` mounts
  `@router.get("/mvp/qhh/latest-product", operation_id="getQhhLatestProduct")`
  under `router = APIRouter(prefix="/api/v1", tags=["forecast"])`.
- Runtime OpenAPI patch: `apps/api/main.py:548` and `apps/api/main.py:568`
  patch the runtime schema for `/api/v1/mvp/qhh/latest-product`.
- Store implementation: `packages/common/forecast_store.py:1056`
  `latest_qhh_display_product(...)`, `packages/common/forecast_store.py:1120`
  `latest_qhh_product_identity(...)`, and QHH latest constants at
  `packages/common/forecast_store.py:14`.
- OpenAPI status: present in `openapi/nhms.v1.yaml:696` with
  `operationId: getQhhLatestProduct`; response uses `QhhLatestProduct` and the
  `basin_id` parameter documents backward compatibility.
- Generated type status: present in `apps/frontend/src/api/types.ts:279` as a
  path entry, `apps/frontend/src/api/types.ts:1118` as `QhhLatestProduct`, and
  `apps/frontend/src/api/types.ts:2428` as operation `getQhhLatestProduct`.
- Backend/test consumers: `tests/test_api_contract.py` covers the success
  envelope, strict identity, and static OpenAPI operation; `tests/test_forecast_api.py`
  covers source-only, basin_id, strict identity, validation, unavailable, and
  unsupported-source behavior; `tests/test_openapi_drift.py` checks runtime vs
  static OpenAPI; `tests/test_readonly_db_validation.py` maps readonly route
  validation to `latest_product`; `tests/test_runtime_mode.py` includes it in
  display-safe route coverage; `tests/test_two_node_e2e_evidence.py` records
  two-node evidence route identity.
- Frontend consumer evidence: `apps/frontend/src/pages/hydroMet/bootstrap.ts:57`
  calls `client.GET('/api/v1/mvp/qhh/latest-product', ...)` for full bootstrap,
  and `apps/frontend/src/pages/hydroMet/bootstrap.ts:75` calls the same route
  with `identity_only=true` for popup product identity. Current consumers fan
  out through `apps/frontend/src/stores/hydroMetProductData.ts`,
  `apps/frontend/src/stores/stationLayerData.ts`,
  `apps/frontend/src/components/map/useHydroMetPopupProduct.ts`,
  `apps/frontend/src/components/map/M11RiverForecastPopup.tsx`, and
  `apps/frontend/src/components/map/M11StationForcingPopup.tsx`.
- Frontend test evidence: `apps/frontend/src/pages/hydroMet/__tests__/bootstrap.test.ts`
  asserts direct latest-product calls and strict-identity behavior;
  `apps/frontend/src/pages/m11/__tests__/useHydroMetProduct.test.tsx` asserts
  latest-product is called once; map popup tests mock `fetchHydroMetLatestProduct`.
- E2E/mocked evidence: current `apps/frontend/e2e` specs do not contain an
  explicit `latest-product` string, but broad mocked `page.route('**/api/v1/**')`
  handlers remain in `preview-deeplink.spec.ts`, `monitoring.spec.ts`,
  `m15-visual-conformance.spec.ts`, and `m11-routes.spec.ts`. Historical
  `docs/runbooks/qhh-mvp-smoke-evidence.md` records mocked `/api/v1/**`
  hydro-met evidence that proved latest-product bootstrap.
- Docs/runbook references: current runbooks and plans include
  `docs/runbooks/two-node-production-e2e-plan.md`,
  `docs/runbooks/node-27-bringup-checklist.md`,
  `docs/runbooks/qhh-mvp-production-like-e2e-checklist.md`,
  `docs/runbooks/qhh-mvp-smoke-evidence.md`,
  `docs/bugs.md`, `docs/plans/2026-05-25-mvp-launch-plan.md`,
  `docs/plans/2026-05-27-two-node-readonly-display-refactor-plan.md`, and
  `docs/plans/2026-05-27-two-node-docker-readonly-display-deployment-plan.md`.
- Replacement/deprecation stance: no replacement endpoint is documented or
  implemented in #411. Keep active compatibility. Do not add deprecation
  headers, remove the route, contract OpenAPI, or regenerate types until
  #413/#414/#415 provide migration evidence.
- Removal readiness: not removal-ready. Active compatibility endpoints are not
  dead code while current repository consumers exist.

### `GET /api/v1/basin-versions/{basin_version_id}/river-segments/{segment_id}/forecast-series`

- Route definition: `apps/api/routes/forecast.py:31` mounts the route under
  `/api/v1`.
- Store implementation: `packages/common/forecast_store.py` implements
  `forecast_series(...)` and related query helpers.
- OpenAPI status: present in `openapi/nhms.v1.yaml:504`.
- Generated type status: present in `apps/frontend/src/api/types.ts:228`.
- Backend/test consumers: `tests/test_forecast_api.py`,
  `tests/test_api_contract.py`, `tests/test_openapi_drift.py`,
  `tests/test_e2e.py`, `tests/test_e2e_ifs.py`, `tests/test_hindcast.py`,
  `tests/test_flood_alerts_api.py`, `tests/test_real_database_integration.py`,
  and `tests/test_production_e2e_validation.py`.
- Frontend consumer evidence: `apps/frontend/src/stores/forecast.ts:377`,
  `apps/frontend/src/stores/overviewData.ts:908`,
  `apps/frontend/src/stores/overviewData.ts:917`, and
  `apps/frontend/src/lib/hydroMet/riverForecast.ts:121` call the generated
  `forecast-series` path.
- Frontend test evidence: `apps/frontend/src/pages/hydroMet/__tests__/bootstrap.test.ts`
  asserts generated forecast-series calls; `apps/frontend/src/stores/__tests__/overviewData.test.ts`
  contains many forecast-series path assertions.
- E2E/mocked evidence: `apps/frontend/e2e/m11-routes.spec.ts:276` and
  `apps/frontend/e2e/m15-visual-conformance.spec.ts:563` branch on URL
  pathnames ending with `/forecast-series`.
- Docs/runbook references: `docs/runbooks/qhh-backend-smoke.md`,
  `docs/runbooks/qhh-mvp-production-like-e2e-checklist.md`,
  `docs/runbooks/qhh-mvp-smoke-evidence.md`,
  `docs/runbooks/two-node-production-e2e-plan.md`,
  `docs/plans/2026-05-25-mvp-launch-plan.md`,
  `docs/plans/2026-05-27-two-node-readonly-display-refactor-plan.md`, and
  `docs/spec/04_api_design.md`.
- Replacement/deprecation stance: no deprecation or replacement is identified
  by #411. This is the canonical current forecast-series API. It should remain
  active while repository consumers exist.
- Removal readiness: not removal-ready.

### Docs-only shorthand forecast-series family

- Candidate forms:
  - `GET /api/v1/river-segments/{segment_id}/forecast-series`
  - `GET /api/v1/river-segments/{id}/forecast-series`
  - relative `/river-segments/{segment_id}/forecast-series`
- Route definition: absent from `apps/api/routes/forecast.py` and broad route
  searches. The current route requires `basin_version_id` and
  `river_network_version_id`.
- OpenAPI status: absent from `openapi/nhms.v1.yaml`; the static OpenAPI only
  contains `/api/v1/basin-versions/{basin_version_id}/river-segments/{segment_id}/forecast-series`.
- Generated type status: absent from `apps/frontend/src/api/types.ts`.
- Backend/test consumers: no current backend route test uses these shorthand
  forms as active API contracts.
- Frontend consumer evidence: no direct frontend `client.GET` call uses these
  shorthand forms. Frontend code uses the generated canonical route.
- E2E/mocked evidence: no direct shorthand route mock found; E2E code branches
  on path suffix `/forecast-series`, which also matches the canonical route.
- Docs/runbook references: `docs/appendices/E_api_openapi_draft.md:30`,
  `docs/modules/13_api_backend_design.md:51`,
  `docs/modules/13_api_backend_spec.md:65`, and
  `docs/spec/06_frontend_gis_design.md:681`.
- Replacement/deprecation stance: replacement-ready documentation cleanup. The
  replacement is the canonical active
  `/api/v1/basin-versions/{basin_version_id}/river-segments/{segment_id}/forecast-series`
  contract. Do not add runtime deprecation headers for a route that does not
  exist.
- Removal readiness: not removal-ready for docs cleanup until #415/#416 update
  or explicitly retain those historical references.

## Reviewed Non-Candidates

- `/api/v1/forecast` appears as a forbidden route prefix in
  `tests/test_role_boundary_static.py`; no active route/OpenAPI/generated type
  was found.
- `POST /api/v1/runs/forecast` appears in module design/spec documents as
  forecast pipeline planning text, not as a current forecast/latest-product
  display compatibility API route.
- `/mvp/qhh/latest-product` without the `/api/v1` prefix appears in some
  commands where `NHMS_API_BASE_URL` is expected to include `/api/v1`; it maps
  to the active `/api/v1/mvp/qhh/latest-product` contract rather than a second
  route.

## Follow-Up Mapping

- #413: backend/test migration only if a replacement is selected for an active
  route. #411 found backend consumers for both active runtime routes, so no
  backend contraction can happen first.
- #414: frontend/node-27 migration for current display/bootstrap consumers,
  especially direct latest-product calls in `apps/frontend/src/pages/hydroMet/bootstrap.ts`.
- #415: OpenAPI and generated type synchronization after #413/#414 evidence.
  Also owns documentation synchronization for stale forecast-series shorthand
  references.
- #416: endpoint removal or explicit deferral after #415. No active route in
  this inventory is removal-ready today.
