## Why

The current station display path relies on list/GeoJSON-style station payloads that do not scale to nationwide tens-of-thousands station overlays. A backend Mapbox Vector Tile route is needed so node-27 can later switch the station layer without pulling a large station inventory into the browser.

## What Changes

- Add a canonical backend `.pbf` tile route for meteorological station points.
- Extend the MVT SQL/helper layer with a PostGIS point-tile query over the station inventory.
- Add OpenAPI contract coverage for the route, response headers, and source-layer properties.
- Preserve existing `/api/v1/met/stations` and station series APIs; frontend consumer migration is explicitly out of scope.

## Capabilities

### New Capabilities

- `station-mvt-tiles`: Backend station point vector tiles for display-ready meteorological station overlays.

### Modified Capabilities

- None.

## Impact

- `services/tiles/mvt.py`
- `apps/api/routes/flood_alerts.py` and runtime OpenAPI patching in `apps/api/main.py`
- `openapi/nhms.v1.yaml`
- API/MVT tests under `tests/`
- No database migration, frontend implementation, or node-27 live validation in this PR.
