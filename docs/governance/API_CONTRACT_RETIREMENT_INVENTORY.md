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
rg -n 'page\.route\(|client\.GET\(|fetchHydroMetLatestProduct|latest-product|forecast-series' apps/frontend/src apps/frontend/e2e || true
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
- Backend/test consumers: `tests/test_api_contract_pipeline_ops.py` covers the
  success envelope and strict identity and `tests/test_api_contract.py` the
  static OpenAPI operation (#2074 partitioned the one file into three); `tests/test_forecast_api.py`
  covers source-only, basin_id, strict identity, validation, unavailable, and
  unsupported-source behavior; `tests/test_openapi_drift.py` checks runtime vs
  static OpenAPI; `tests/test_readonly_db_validation.py` maps readonly route
  validation to `latest_product`; `tests/test_runtime_mode.py` includes it in
  display-safe route coverage.
- Frontend consumer evidence: `apps/frontend/src/stores/hydroMetBootstrap.ts:32`
  calls `client.GET('/api/v1/mvp/qhh/latest-product', ...)` with
  `identity_only=true` for popup product identity; it is the only frontend call.
  The full (non-`identity_only`) bootstrap call lost its last production caller in
  #3222 and was deleted in #3241. Current consumers fan out through
  `apps/frontend/src/stores/stationLayerData.ts`,
  `apps/frontend/src/components/map/M11RiverForecastPanel.tsx`, and
  `apps/frontend/src/components/map/M11StationForcingPopup.tsx`.
- Frontend test evidence: `apps/frontend/src/pages/hydroMet/__tests__/bootstrap.test.ts`
  asserts the identity-only latest-product call and (`bootstrap.test.ts:323`) that
  it is called once per basin, source and cycle; map popup tests mock
  `fetchHydroMetLatestProduct`.
- E2E/mocked evidence: current `apps/frontend/e2e` specs do not contain an
  explicit `latest-product` string, but broad mocked `page.route('**/api/v1/**')`
  handlers remain in `preview-deeplink.spec.ts`, `monitoring.mocked.spec.ts`,
  `m15-visual-conformance.spec.ts`, and `m11-routes.mocked.spec.ts` (the last
  through `e2e/support/layerCatalog.mocked.ts`; spec names refreshed by #3164). The
  historical runbook that recorded mocked `/api/v1/**` hydro-met evidence for the
  latest-product bootstrap was deleted in #3405 (git keeps it).
- Docs/runbook references: current runbooks and plans include
  `docs/runbooks/two-node-production-e2e-plan.md`,
  `docs/runbooks/node-27-bringup-checklist.md`,
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
  `tests/test_flood_alerts_api.py`, and `tests/test_real_database_integration.py`.
- Frontend consumer evidence: `apps/frontend/src/lib/hydroMet/riverForecast.ts:113` calls the generated
  `forecast-series` path; it is the only production call site (checked 2026-11-20, #3222;
  `stores/overviewData.ts` no longer calls it).
- Frontend test evidence: `apps/frontend/src/pages/hydroMet/__tests__/bootstrap.test.ts`
  asserts the generated forecast-series call and its query (`:804`, `:847`).
- E2E/mocked evidence: at the time of this record
  `apps/frontend/e2e/m11-routes.spec.ts:276` and
  `apps/frontend/e2e/m15-visual-conformance.spec.ts:563` branched on URL
  pathnames ending with `/forecast-series`. Refreshed by #3164: `m11-routes.spec.ts`
  is now `m11-routes.mocked.spec.ts` and neither file carries that branch any
  more; the current branch is `apps/frontend/e2e/support/curveSheet.mocked.ts`
  (used by the curve-sheet mocked specs).
- Docs/runbook references: `docs/runbooks/qhh-backend-smoke.md`,
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
  especially direct latest-product calls in `apps/frontend/src/stores/hydroMetBootstrap.ts`.
- #415: OpenAPI and generated type synchronization after #413/#414 evidence.
  Also owns documentation synchronization for stale forecast-series shorthand
  references.
- #416: endpoint removal or explicit deferral after #415. No active route in
  this inventory is removal-ready today.

## Retirement Records

Records appended after the 2026-06-11 inventory. The sections above remain
the #411 evidence snapshot and are not rewritten.

### `GET /api/v1/basin-versions/{basin_version_id}/river-segments` — retired by #2866

- Status: removed (was never a candidate in the #411 matrix above, which covers
  the forecast/latest-product family only). Decision date: 2026-10-12.
- What was removed: the route (`apps/api/routes/models.py:list_river_segments`),
  its OpenAPI path and the `RiverSegmentFeature` /
  `RiverSegmentFeatureCollection` components (`openapi/nhms.v1.yaml`, the
  runtime patches, `apps/frontend/src/api/types.ts`), the response models that
  only it used, and its one frontend caller (`fetchHydroMetRiverSegments` in
  `apps/frontend/src/stores/hydroMetBootstrap.ts`, whose result no product code
  consumed). No shim, no `410`, no deprecation header: the path answers `404`.
- Why: its features carried a derived id `<model_id>_seg_<iRiv>_<iEle>` that is
  no row of `core.river_segment`; the detail and forecast-series endpoints
  answer `404` for it, and the map renders rivers from the MVT layer, not from
  this GeoJSON list.
- Unchanged: `GET .../river-segments/{segment_id}/forecast-series` (the
  `active` row above), `GET .../river-segments/{segment_id}` (the detail
  endpoint, not in the #411 matrix), import, `core.river_segment_crosswalk`
  writing, MVT. No migration.
- Basis against the external-consumer rule: `API_CONTRACT_DEPRECATION_POLICY.md`
  states that external consumers are unknown and that repository-only evidence
  must not be used to contract OpenAPI. That rule is not relaxed here. For this
  endpoint the unknown was resolved by the owner, not by repository search: the
  owner ruling of 2026-10-12 on #2866 states that the system has no external
  API users and selects retirement. That ruling is the documented
  external-consumer treatment the policy requires; it covers this endpoint only
  and does not change the status of any row in the Candidate Matrix.
- Replacement for clients: `core.river_segment.river_segment_id` values (reach
  ids `<model_id>_reach_<iRiv:06d>` or SHUD output-river ids) with the detail
  and forecast-series endpoints; the river-network MVT tiles for map rendering.
- Rollback: revert the #2866 change; no data or schema was touched.
- Contract evidence: `tests/test_api_contract_resources.py`
  (`test_river_segment_list_endpoint_is_retired_and_not_served`,
  `test_river_segment_list_is_absent_from_the_published_openapi_and_generated_types`,
  `test_river_segment_detail_is_still_served_after_the_list_retirement`).
- Residual, closed by #2907: the store read
  `PsycopgModelRegistryStore.list_river_segments`, its legacy / segment-slice
  helpers, the four `RIVER_SEGMENT_COLLECTION_*` budgets (and their
  `packages.common.model_registry` re-exports) are deleted. The frozen #1913 registry-import partition tests that observed import
  results through the store were recorded in the partition retirements ledger
  (two rewritten to assert the imported rows directly, four retired); the
  partition oracle was unchanged (#3219 later deleted both fixtures with the
  only suite that read them). Besides the
  pre-PR-2 row cleanup `_delete_legacy_seg_rows`, the `basins_seg_shp` crosswalk
  source and `parse_seg_shp_crosswalk`, the only remaining `_seg_` code was the
  caller-less id producer `workers/model_registry/basins_geometry.py::_stable_segment_id`,
  deleted by #3081. `get_river_segment` (detail) and
  `core.river_segment_crosswalk` writing are unchanged.
- Left after #2907, reported and not removed: the trigram index
  `river_segment_id_trgm_idx` (migration 000052, ADR 0004) lost its only
  consumer, the list search's `lower(rs.river_segment_id) LIKE` arm, and is now
  an orphan index; dropping it is a separate DB change. The caller-less helpers
  `_stable_segment_id` and `_deduplicate_pending_segment_ids` in
  `workers/model_registry/basins_geometry.py` were deleted by #3081, which first
  split that 1440-line file into `basins_geometry.py`, `basins_geometry_shapes.py`
  and `basins_geometry_types.py` (each under the large-file guard limit, no new
  exclusion). The file's other caller-less helpers moved unchanged; #3147 later deleted
  them as zero-reference dead code.

### `GET /api/v1/tiles/met-stations/{basin_version_id}/{z}/{x}/{y}.pbf` — retired by #2773 item 4

- Status: removed by PR #3071 (merged 2026-11-06). Owner decision on #2773
  (2026-11-06, restated 2026-11-08): retire the path, do not register the
  station-set flip hook and do not switch the query to latest-run stations.
  This record was added afterwards; the retirement itself is recorded in the
  archived change `openspec/changes/archive/2026-11-06-retire-met-station-mvt-tiles/`
  and `openspec/specs/station-mvt-tiles/spec.md`.
- What was removed: the route `met_station_mvt_tile` (`getMetStationTile`), its
  only helpers (`_station_source_version`, `_station_active_flag`,
  `_fetch_station_mvt_tile_bytes`), the `met-stations` branch of
  `services/tiles/mvt.py`, its OpenAPI path and the generated frontend type.
  No shim, no `410`, no deprecation header: the path answers `404`
  `{"detail": "Not found"}`, same treatment as #2866 above.
- Why: the query listed stations by `met.met_station.active_flag = true`, but no
  production code registered the station-flag flip hook (the module was
  deleted in #3164), so every Direct Grid station is `active_flag = false` (28,926 of 28,926 on
  node-27, 2026-11-08) and the route returned `MVT_SOURCE_IDENTITY_NOT_FOUND`
  for every basin. The frontend never called it.
- Unchanged: `GET /api/v1/met/stations` (station list), the `met-stations` entry
  of `/api/v1/layers` (it never advertised a tile URL), river and hydro MVT tiles.
- External-consumer basis: same owner ruling as #2866 (no external API users);
  it covers this endpoint only.
- Replacement for clients: `GET /api/v1/met/stations`.
- Rollback: revert PR #3071; no data or schema was touched.
- Left, reported and not removed: the station-flag flip hook module (only
  tests and archived rehearsal evidence imported it; #3164 deleted it with its
  tests and selector rules) and the index `met_station_active_basin_station_idx`
  (migration 000033), whose only reader was this route. The index is still
  kept, because dropping it needs a node-27 DB write.
- Contract evidence: `tests/test_met_station_mvt_route_retired.py`.
