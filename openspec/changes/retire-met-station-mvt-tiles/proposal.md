# Retire the station vector-tile route

## Why

Owner decision on #2773 item 4 (2026-11-06). `GET /api/v1/tiles/met-stations/{basin_version_id}/{z}/{x}/{y}.pbf`
listed `met.met_station` rows with `active_flag = true`. Every Direct Grid station is `active_flag = false`, so on
node-27 the route answered 404 `MVT_SOURCE_IDENTITY_NOT_FOUND` for every Direct Grid basin (measured read-only:
`/api/v1/tiles/met-stations/basins_qhh_vbasins/5/24/12.pbf`). Nothing used it: the frontend only uses `met-stations`
as a MapLibre layer id and draws stations from `GET /api/v1/met/stations`, and the `nhms-display-api` journal shows 0
requests to the tile path in 7 days. The owner rejected the two alternatives: registering the `station_set_flip` hook
(flips production `active_flag`) and resolving stations by run (new code for a path the frontend does not call).

## What Changes

- **BREAKING (no known consumer)**: remove the route `GET /api/v1/tiles/met-stations/{basin_version_id}/{z}/{x}/{y}.pbf`
  (`getMetStationTile`). The URL becomes an unknown path (router 404).
- Remove the code only that route used: `_station_source_version` / `_station_active_flag`
  (`apps/api/routes/hydro_display_identity.py`), `_fetch_station_mvt_tile_bytes`
  (`apps/api/routes/hydro_display_postgis.py`), and the `met-stations` branches of `services/tiles/mvt.py`
  (tile SQL, tile columns, order-by, source-layer map, `map.tile_layer` metadata).
- Remove the path from `openapi/nhms.v1.yaml`, from the OpenAPI MVT patch list, and from the generated frontend types.
- Keep the `met-stations` entry of `GET /api/v1/layers`: it never advertised a tile URL (its metadata is the
  `geojson_compatibility` stub) and the frontend builds a layer state from every catalog entry.
- Keep `GET /api/v1/met/stations`, the station write path, `packages/common/station_set_flip.py` and all
  `active_flag` data unchanged.

## Capabilities

### Modified Capabilities

- `station-mvt-tiles`: the route requirements are removed and replaced by a retirement requirement.
- `national-river-density`, `postgis-tile-clipping-cache`: drop `met-stations` from the per-layer budget lists.
- `direct-grid-variant-registration`, `fixed-station-forcing-production`, `direct-grid-readiness-evidence`: the
  no-mixed-display scenarios name the active station set (`basin_version_id + active_flag = true`) instead of the
  retired station-MVT query.

## Impact

- API: one route and one OpenAPI operation removed; no other route, envelope or error contract changes.
- Frontend: `apps/frontend/src/api/types.ts` regenerated; no source change (it never requested the route).
- Tests: tests that only covered the route or `_station_source_version` are deleted; the `met-stations` rows of the
  MVT layer-parameter tests are dropped; `tests/test_met_station_mvt_route_retired.py` pins the retirement.
- Not reconciled here: the active change `direct-grid-display-cutover` still names `_station_source_version` in its
  tasks 1.1 / 1.2 / 3.1 evidence and in its `active-model-dynamic-resolution` delta ("station-MVT tile cache key").
  That change is not archived, so its delta is not in `openspec/specs/` yet; it must be reconciled before it is
  archived. The tests it cites for that evidence were deleted with the code.
- Existing `map.tile_layer` / `map.tile_cache` rows for `met-stations` stay in the database; they are no longer
  read or written.
