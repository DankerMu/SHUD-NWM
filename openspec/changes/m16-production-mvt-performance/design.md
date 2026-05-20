## Context

M16 production MVT and national performance follows the completed M11 overview/basin drill-down delivery and turns a documented product gap into implementable, testable work. Existing production-like closure and M11 behavior must remain stable.

## Fixture

Issue type: backend/frontend/contract/performance feature.
Project profile: other - FastAPI backend, OpenAPI contract, PostGIS-oriented tile service, React/MapLibre frontend.
Blast radius: high production-facing map/API surface.
Fixture level: expanded.
Repair intensity: broad-expanded, because the issue spans public tile APIs, OpenAPI/types, database SQL and schema metadata, cache read/write/invalidation, frontend national rendering behavior, performance evidence, and production readiness semantics.

Mandatory expanded triggers:

- Public API contract and content type behavior for `.pbf` tile endpoints.
- Database/SQL behavior, including PostGIS clipping, simplification, cache metadata, and invalidation.
- File/evidence writes for deterministic and opt-in live performance reports.
- Resource-limit and payload-size controls for national map data.
- Frontend map rendering path that must not silently fall back to full-national GeoJSON.
- Release-readiness semantics that must distinguish deterministic evidence, live proof, compatibility mode, and release blockers.

## Change Surface

- OpenAPI tile paths and generated frontend API types when API schemas change.
- Backend tile routes/services for hydrology MVT, flood-return-period MVT, validation errors, cache headers, and legacy GeoJSON compatibility.
- Database schema or migration surfaces for `map.tile_layer` and `map.tile_cache` metadata if existing columns are insufficient.
- PostGIS SQL builders/helpers that produce `ST_TileEnvelope`, clipping, simplification, `ST_AsMVTGeom`, and `ST_AsMVT` behavior.
- Frontend MapLibre hydrology/flood-return-period layer selection and user-visible compatibility status.
- Production scale validation artifacts and docs.

## Must Preserve

- Existing GeoJSON compatibility endpoint semantics for bounded/small/degraded views.
- Existing M11-M15 frontend routes, visual evidence, valid-time/timeline behavior, and no-overlap/accessibility gates.
- Existing production-like evidence honesty: do not claim final live readiness when real PostGIS/national data/dependencies are missing.
- Existing OpenAPI drift policy unless updated with a precise M16 reason.
- Meteorology raster/grid products remain separate from hydrology vector MVT.

## Design Decisions

- MVT endpoints must return `application/x-protobuf` and encode named vector layers with required feature properties: segment_id/river_segment_id, basin_version_id, river_network_version_id, value/unit where applicable, return_period, warning_level, quality_flag, source/cycle/valid_time metadata or references.
- Exact public path disposition:
  - `/api/v1/tiles/river-network/{basin_version_id}/{z}/{x}/{y}.pbf` becomes the canonical river-network MVT path.
  - `/api/v1/tiles/hydro/{run_id}/{variable}/{valid_time}/{z}/{x}/{y}.pbf` becomes the canonical hydrological-output MVT path.
  - `/api/v1/tiles/flood-return-period/{run_id}/{duration}/{valid_time}/{z}/{x}/{y}.pbf` is promoted from legacy compatibility redirect to true flood-return-period MVT only when the implementation satisfies the contract; until then, the query GeoJSON endpoint remains compatibility-only and must not be advertised as national MVT.
  - `/api/v1/tiles/flood-return-period` remains the bounded GeoJSON compatibility endpoint with bbox/feature/coordinate/payload budgets.
- PostGIS SQL must use tile envelope clipping/simplification and avoid loading full national FeatureCollections into application memory.
- `map.tile_layer` stores layer metadata and tile URL templates; `map.tile_cache` records cached tile status/checksum/etag when caching is enabled.
- Frontend uses vector sources for national hydrology layers when available and explicitly labels fallback mode.
- Frontend MVT selection must be driven by a layer metadata discovery contract, not hard-coded route guesses. The metadata surface may be an existing API extension or a new endpoint, but it must expose `layer_id`, `tile_format`, URL template with `{z}/{x}/{y}` plus required source/time placeholders, MapLibre `source-layer` id, property schema/version, min/max zoom, Web Mercator bounds, available valid_times or source references, cache etag/version, and fallback/release-blocking flags.
- MVT z/x/y semantics use standard Web Mercator XYZ tiles (`EPSG:3857`) with 256 px tile size unless explicitly documented otherwise. Source geometries may remain in their storage CRS, but SQL must transform to Web Mercator before MVT geometry encoding, clip to the tile envelope, use documented extent/buffer, and validate encoded coordinates/geometries are within MVT extent/buffer bounds.
- Deterministic CI may validate SQL plans/builders, fake encoded tile bytes, cache contracts, OpenAPI/type freshness, frontend source selection, and bounded evidence. Real PostGIS/national-data execution is opt-in and must record `not_executed` or `release_blocked` when dependencies are absent.
- MVT endpoints must fail before expensive SQL for invalid z/x/y, invalid valid_time/run/layer identifiers, unsupported variables/durations, or budget settings.
- Cache keys must include layer identity, source/run/version identity, valid_time, z/x/y, style-affecting parameters, and schema/encoder version so stale cached PBF cannot be served across incompatible inputs.
- The implementation may use a small deterministic MVT encoder abstraction or fixture bytes for unit tests, but public API evidence must still verify `application/x-protobuf`, layer metadata, cache identity, and no full-national GeoJSON requests.
- Readiness summary status must remain compatible with current production scale validation: existing `ready`/`blocked` and `production_mvt_readiness_claimed` semantics are preserved unless a deliberate schema migration is fully tested. A deterministic MVT pass may set deterministic tile evidence to `ready`/`passed`, but final production readiness remains not claimed when live PostGIS/national-data proof is `not_executed`; release blockers must include blocker id, surface, status, removal criteria, residual risk, affected endpoints, and artifact links.

## Dependency Order

- OpenAPI/tile contract before backend implementation.
- PostGIS/cache implementation before frontend MVT migration.
- Frontend migration before national performance evidence.
- Fallback compatibility after primary MVT path is validated.

## Risks and Mitigations

- Risk: SQL resource exhaustion. Mitigation: z/x/y bounds, feature/coordinate budgets, query timeout and plan evidence.
- Risk: silent fallback to GeoJSON. Mitigation: UI/test exposes MVT vs fallback mode and national views require MVT.
- Risk: cache staleness. Mitigation: layer version/run_id/valid_time keys and invalidation tests.
- Risk: fake readiness. Mitigation: deterministic lane records current proof and live lane records `not_executed` or `release_blocked` unless target PostGIS/national data is actually configured.
- Risk: OpenAPI/runtime drift. Mitigation: update OpenAPI and generated frontend types together, or document unchanged legacy paths with tests.

## Route, Contract, and Evidence Matrix

Required API contract examples:

| Surface | Required behavior |
|---|---|
| River-network MVT | `/api/v1/tiles/river-network/{basin_version_id}/{z}/{x}/{y}.pbf` returns `application/x-protobuf`, layer id, and cacheable headers for valid tile inputs. |
| Hydrological-output MVT | `/api/v1/tiles/hydro/{run_id}/{variable}/{valid_time}/{z}/{x}/{y}.pbf` returns `application/x-protobuf`, layer id, and source/time/value metadata. |
| Flood return-period MVT | `/api/v1/tiles/flood-return-period/{run_id}/{duration}/{valid_time}/{z}/{x}/{y}.pbf` returns true MVT when promoted; bounded query GeoJSON remains compatibility only. |
| Layer metadata discovery | API exposes tile format, URL template, source-layer id, property schema/version, zoom/bounds, valid-time/source refs, etag/version, and fallback/release-blocking flags. |
| Invalid z/x/y/query | stable 4xx validation envelope without invoking expensive SQL/builders. |
| Cache hit/miss | idempotent tile cache rows, deterministic checksum/etag, and invalidation by layer/source/version/valid_time. |
| Oversized tile | bounded degraded/error response with evidence and no unbounded application memory FeatureCollection. |

Required frontend evidence:

| Scenario | Required behavior |
|---|---|
| MVT metadata available | MapLibre registers vector source/layers and does not request full-national GeoJSON. |
| MVT unavailable for national view | UI shows release-blocking/unavailable MVT status instead of silently loading full-national GeoJSON. |
| Small/degraded bbox fallback | GeoJSON compatibility path remains bounded, labeled, and limited by bbox/feature budget. |

Required performance evidence fields:

- `execution_mode`: `deterministic_fixture`, `live_postgis`, or `not_executed`.
- `status`: preserve current validation vocabulary (`ready`, `blocked`) where consumed by existing production scale summary; new detailed records may additionally expose `passed`, `failed`, `not_executed`, or `release_blocked` only with schema/tests and mapping to the existing summary fields.
- `query_plan_hash`, `sql_shape_hash`, p95 latency, payload bytes, tile count, feature count, coordinate count, browser timing, memory proxy, thresholds, blockers, and artifact paths.
- Live proof must include dependency metadata and redacted connection/source identifiers without credentials.
- `production_mvt_readiness_claimed` must remain false unless deterministic MVT checks and opt-in live PostGIS/national-data/frontend proof have all passed.
- Blocker entries must include `blocker_id`, `surface`, `status`, `affected_endpoints`, `removal_criteria`, `residual_risk`, and `artifact_links`.

## Boundary Surface Checklist

- Shared helper roots: tile route validation, SQL builder, cache key builder, evidence writer, frontend map source selection.
- Public entrypoints: OpenAPI tile paths, legacy GeoJSON endpoint, frontend national map routes.
- Read surfaces: model/river/flood source metadata, tile layer/cache metadata, generated API types.
- Write surfaces: tile cache rows, deterministic evidence artifacts, opt-in live evidence artifacts.
- Stale-state/idempotency boundaries: repeated tile requests, cache invalidation, layer version changes, valid_time/source switches, frontend state restore.
- Unchanged downstream consumers: M10 production-like validation, M11/M15 visual gates, bounded GeoJSON clients, monitoring/docs.

## Risk Packs Considered

- Public API / CLI / script entry: selected - public tile endpoints and validation commands are entrypoints.
- Config / project setup: selected - CI/evidence commands and optional live env gates may change.
- File IO / path safety / overwrite: selected - performance/evidence artifacts and cache/evidence paths are written.
- Schema / columns / units / field names: selected - OpenAPI, generated types, tile feature properties, and map metadata must remain stable.
- Geospatial / CRS / shapefile sidecars: selected - tile envelope, clipping, simplification, bounds, layer extent, and geometry validity are core behavior.
- Time series / forcing / temporal boundaries: selected - valid_time/run/cycle keys define tile identity and invalidation.
- Numerical stability / conservation / NaN: selected - return_period/value/unit/quality fields must not silently change semantics or emit non-finite values.
- Solver runtime / performance / threading: not selected - no SHUD solver runtime changes.
- Resource limits / large input / discovery: selected - national-scale tiles require strict budgets and bounded evidence.
- Legacy compatibility / examples: selected - GeoJSON compatibility remains bounded and truthful.
- Error handling / rollback / partial outputs: selected - invalid/oversized/cache failure paths must be explicit and not publish partial misleading evidence.
- Release / packaging / dependency compatibility: selected - OpenAPI/type generation, frontend build/tests, optional PostGIS mode must remain CI-compatible.
- Documentation / migration notes: selected - validation docs and progress must explain MVT readiness and live-data limits.

## Verification

- OpenSpec strict validation.
- Backend focused pytest and real PostGIS opt-in tests.
- OpenAPI/type freshness.
- Frontend E2E no-full-national-GeoJSON and vector source assertions.
- Performance evidence artifact with p95/payload/memory/browser timing thresholds.
