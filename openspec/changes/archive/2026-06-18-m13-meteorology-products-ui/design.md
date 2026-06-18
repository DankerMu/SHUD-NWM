## Context

M13 meteorology products UI follows the completed M11 overview/basin drill-down delivery and turns a documented product gap into implementable, testable work. Existing production-like closure and M11 behavior must remain stable.

## Fixture

Fixture level: expanded
Project profile: other
Repair intensity: medium
Blast radius: high user-visible frontend route plus API/type contracts; not high-risk for file IO/auth/publish because no credential, artifact publish, or destructive operation is in scope.

Mandatory expanded triggers:
- Public route/nav and query-state compatibility for `/meteorology`.
- Meteorology schema/field/unit/source contracts for grid metadata, station inventory, station series, QC, and provenance.
- Geospatial bbox, resolution, lon/lat, station marker, and map-popup behavior.
- Temporal forcing, valid-time, native-resolution, stale-time correction, and playback behavior.
- Restricted/unavailable/error states for CLDAS, tile failures, empty valid times, missing stations, missing forcing, unsupported comparison, and over-limit requests.
- Legacy route/API compatibility for M11, segment detail, existing station, station-series, and best-available consumers.
- Browser visual evidence for both meteorology tabs because the issue implements effect-image pages.

Change surface:
- Frontend route/nav/query state for `/meteorology?tab=grid|stations`.
- Meteorology grid metadata, tile/query, timeline, comparison, unavailable/restricted state, and station overlay consumers.
- Meteorology station inventory, selection, popup, forcing/QC chart, adjacent-station, and empty-state consumers.
- API/OpenAPI/type contracts only if existing station/best-available contracts are insufficient.
- `progress.md` status text for effect images 5 and 6.

Must preserve:
- M11 overview/basin routes and their unavailable meteorology placeholders keep working until `/meteorology` contracts are reachable.
- Existing `/api/v1/met/stations`, `/api/v1/met/stations/{station_id}/series`, and `/api/v1/met/best-available` consumers keep response compatibility if backend code is touched.
- Missing, restricted, or failed meteorology products never fall back to fabricated values or generated time sequences.

## Design Decisions

- First define a renderer-neutral metadata contract: variable, source, cycle_time, valid_time, native_time_resolution, spatial_resolution, unit, bbox, tile URL template, and restricted/unavailable reason.
- Raster rendering may use TiTiler, pre-generated PNG tiles, or another backend behind the same metadata contract; implementation must choose and document one path before adding backend endpoints. If no live tile service exists, the UI must render explicit unavailable overlays while preserving tile URL metadata semantics.
- Station IDs remain stable `station_id`; station forcing charts must show source provenance and QC completeness.
- CLDAS is a restricted source until credentials/proof exist; UI must show restricted state rather than silently substituting values.
- The timeline reads valid times from metadata/series contracts only. Empty valid-time arrays, stale selected times, and source switches reset to a contract-provided valid time or a visible unavailable state.
- Multi-source comparison is enabled only for comparable variable/time/source combinations supplied by metadata. Unsupported comparison shows a scoped unavailable state rather than computing from missing data.

## Dependency Order

- Navigation/contracts before grid or station page.
- Grid metadata/tile/query before grid map controls.
- Station inventory/forcing/QC before station detail UI.

## Risks and Mitigations

- Risk: M13 overlaps with M16 tile infrastructure. Mitigation: M13 owns meteorology raster/grid/station products; M16 owns hydrology vector MVT.
- Risk: CLDAS missing blocks UI. Mitigation: restricted-state scenarios and tests.
- Risk: large raster queries. Mitigation: tile/query bounds and area-stat request limits.
- Risk: API/type drift for station and best-available contracts. Mitigation: OpenAPI/type freshness checks whenever backend contracts are added or changed.
- Risk: stale raster or station detail state survives source/tab/station changes. Mitigation: selected-time correction, stale tile cleanup tests, station-selection regression tests, and no-stale-popup assertions.

## Risk Packs Considered

- Public API / CLI / script entry: selected - `/meteorology` is a new public frontend route and backend API/OpenAPI may be extended.
- Config / project setup: not selected - no new deployment or credential configuration is required.
- File IO / path safety / overwrite: not selected - this issue does not read/write local files at runtime or publish artifacts.
- Schema / columns / units / field names: selected - variable/source/unit/bbox/time/QC fields are contract-critical.
- Geospatial / CRS / shapefile sidecars: selected - grid bbox/resolution and station lon/lat/map marker behavior are user-visible geospatial contracts; no shapefile sidecars are added.
- Time series / forcing / temporal boundaries: selected - valid times, station forcing series, native resolution, stale correction, playback, and QC intervals are central.
- Numerical stability / conservation / NaN: not selected - no solver math or numerical model outputs are computed.
- Solver runtime / performance / threading: not selected - no SHUD runtime or threaded processing is changed.
- Resource limits / large input / discovery: selected - tile/query/area-stat calls, station inventory pagination, station search/filter result counts, and forcing series time-range/sample rendering need bounded request and rendering behavior.
- Legacy compatibility / examples: selected - existing M11 placeholders, routes, and API consumers must remain compatible.
- Error handling / rollback / partial outputs: selected - restricted, unavailable, failed tile/query, empty station, and forcing unavailable states are primary acceptance paths.
- Release / packaging / dependency compatibility: selected - frontend dependency/build/type checks must pass; avoid unnecessary new map/chart dependencies, or document any named dependency with rationale.
- Documentation / migration notes: selected - `progress.md` must accurately state enabled scope and live-data limits.

## Boundary Surface Checklist

- Public entrypoints: `/meteorology`, nav item, tab query state, API endpoints if added, OpenAPI generated types if refreshed.
- Read surfaces: grid metadata, tile/query response, area statistics, station inventory, station series/QC, best-available provenance.
- Stale-state/idempotency boundaries: tab switches, variable/source switches, valid-time correction, tile failure cleanup, station row/marker selection, adjacent-station toggles.
- Unchanged downstream consumers: M11 overview/basin pages, segment detail station/forcing widgets, existing forecast API tests.

## Verification

- OpenSpec strict validation.
- API/OpenAPI/type checks if backend contracts are added.
- Frontend unit/type/build tests for grid route, station route, unavailable states, CLDAS restricted state, stale-time correction, station selection, partial QC, and forcing unavailable.
- Dependency decision evidence: no new map/chart dependency, or named dependency rationale plus lockfile/install/type/build/test checks.
- Browser smoke evidence for both `/meteorology?tab=grid` and `/meteorology?tab=stations`, using agent-browser or Playwright screenshots after implementation.
