## 1. Retire the route and its code

- [x] 1.1 Remove `met_station_mvt_tile` (`GET /api/v1/tiles/met-stations/{basin_version_id}/{z}/{x}/{y}.pbf`) from
  `apps/api/routes/hydro_display.py`.
- [x] 1.2 Remove `_station_source_version` and `_station_active_flag` (`apps/api/routes/hydro_display_identity.py`) and
  `_fetch_station_mvt_tile_bytes` (`apps/api/routes/hydro_display_postgis.py`).
- [x] 1.3 Remove the `met-stations` branches of `services/tiles/mvt.py` (tile SQL, tile columns, order-by,
  `_source_layer_id`, `_ensure_tile_layer` / `_cache_layer_metadata`); river/basin/hydro branches unchanged.
- [x] 1.4 Keep the `met-stations` `/api/v1/layers` entry (no tile URL advertised; frontend reads every entry),
  `/api/v1/met/stations`, `packages/common/station_set_flip.py` and `active_flag` data unchanged.
- [x] 1.5 Confirm no tile warmer or cache script requests station tiles (`grep -rn "met-stations" scripts infra`: none).

## 2. Contract

- [x] 2.1 Remove the path from `openapi/nhms.v1.yaml` and from `_patch_mvt_tile_openapi`.
- [x] 2.2 Regenerate `apps/frontend/src/api/types.ts` (`pnpm generate:api`); `pnpm check:api-types` passes.

## 3. Tests and docs

- [x] 3.1 Delete tests that only covered the route or `_station_source_version`; drop `met-stations` from the MVT
  layer-parameter tests; re-anchor the `hydro_display` importer closure in `tests/test_select_ci_tests.py`.
- [x] 3.2 Add `tests/test_met_station_mvt_route_retired.py`: router 404 (not `MVT_SOURCE_IDENTITY_NOT_FOUND`), absent
  from static and runtime OpenAPI, `/api/v1/met/stations` still answers.
- [x] 3.3 Update `docs/spec/04_api_design.md` and `docs/runbooks/node-27-bringup-checklist.md`.

## Evidence Floor

- [x] E1 Red run of `tests/test_met_station_mvt_route_retired.py` against pre-change source (404
  `MVT_SOURCE_IDENTITY_NOT_FOUND`), green after.
- [x] E2 `uv run python scripts/select_ci_tests.py --base-ref origin/master` selection passes; `uv run ruff check .` passes.
- [x] E3 `cd apps/frontend && pnpm check:api-types && pnpm typecheck && pnpm test` pass.
- [x] E4 `openspec validate retire-met-station-mvt-tiles --strict --no-interactive` passes.
- [ ] E5 node-27 live receipt after merge (orchestrator): the tile URL answers router 404 and
  `/api/v1/met/stations?basin_version_id=basins_qhh_vbasins` still returns stations.
