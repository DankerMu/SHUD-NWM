## 1. Navigation and Contracts
- [x] 1.1 Define the `/meteorology` route, tab query state, and nav visibility rule.
- [x] 1.2 Define meteorology metadata contracts for variables `PRCP`, `TEMP`, `RH`, `wind`, `Rn`, `Press`, sources `GFS`, `IFS`, `ERA5`, `CLDAS`, `Best Available`, valid times, units, bbox, resolution, tile/query URLs, and restricted reasons.
- [x] 1.3 Decide raster renderer path (TiTiler, pre-generated PNG tiles, or abstraction) and document endpoint decisions before backend changes.
- [x] 1.4 If backend contracts change, refresh/verify OpenAPI and frontend API types without breaking existing `/api/v1/met/stations`, `/api/v1/met/stations/{station_id}/series`, or `/api/v1/met/best-available` consumers.
- [x] 1.5 Document dependency decision: prefer existing map/chart/UI utilities; if adding a dependency, name it, justify it, update the lockfile intentionally, and verify install/type/build/test.

Implementation note: M13 uses a frontend renderer-neutral fixture contract and explicit unavailable overlays for live raster tiles/query endpoints. Backend/OpenAPI contracts were not changed, preserving existing `/api/v1/met/stations`, `/api/v1/met/stations/{station_id}/series`, and `/api/v1/met/best-available` consumers. Dependency decision: no new dependency; reused React Router, Tailwind, lucide-react, ECharts, and existing M11 layout/map visual conventions.

## 2. Grid Product Page
- [x] 2.1 Implement grid layer metadata loading, timeline ticks, stale valid-time correction, and restricted/missing states.
- [x] 2.2 Implement variable/source controls, opacity, contour toggle, station overlay toggle, legend/color scale, grid-cell query popup, area statistics, and multi-source comparison panel.
- [x] 2.3 Add tests for tile failure, CLDAS restricted state, unsupported comparison, empty valid times, stale tile cleanup, and valid-time reset after source/variable switches.
- [x] 2.4 Bound grid-cell query and area-stat requests by contract-provided bbox/resolution/limit metadata; show scoped unavailable/error states for over-limit or missing products.

## 3. Station Query Page
- [x] 3.1 Implement station inventory contract, basin filter, search, sort, completeness/QC status, and adjacent-station data.
- [x] 3.2 Implement station map markers, popup, detail panel, forcing charts for PRCP/TEMP/RH/wind/Press, QC markers, and no-station empty state.
- [x] 3.3 Add tests for station selection from row and marker, popup/detail synchronization, partial QC, forcing unavailable, no-station search/filter, and adjacent-station highlighting.
- [x] 3.4 Ensure station detail clears stale forcing/QC when station, basin filter, or tab state changes.
- [x] 3.5 Bound station inventory and station-series rendering by contract/page-size/time-range/sample limits; show empty, truncated, or validation states for over-limit inputs instead of rendering unbounded payloads.

## 4. Validation
- [x] 4.1 Run OpenSpec strict validation plus focused API/OpenAPI/type checks if contracts are added.
- [x] 4.2 Run frontend unit tests, `tsc --noEmit`, build, and Playwright coverage for both tabs.
- [x] 4.3 Update `progress.md` with enabled meteorology scope and remaining live-data limitations.
- [x] 4.4 Capture browser smoke evidence for `/meteorology?tab=grid` and `/meteorology?tab=stations` showing non-overlapping layout and explicit restricted/unavailable states where applicable.

Validation evidence: `openspec validate m13-meteorology-products-ui --strict --no-interactive`, `corepack pnpm exec tsc --noEmit`, `corepack pnpm test -- --runInBand`, `corepack pnpm build`, and `corepack pnpm exec playwright test e2e/meteorology.spec.ts --project=chromium` passed. Focused Playwright coverage visits `/meteorology?tab=grid&source=CLDAS&variable=PRCP` and `/meteorology?tab=stations&basin=yangtze&stationId=HMT-Y2-0237` without backend API dependency, asserting route/nav/tab restore, CLDAS restricted/unavailable state with disabled timeline, station inventory, selected station popup/marker, adjacent stations, and forcing chart rendering.

## Evidence Matrix

- Public route/nav: visit `/meteorology?tab=grid` and `/meteorology?tab=stations` -> selected tab and relevant state restore from the URL; existing routes still render.
- Schema/units/source contract: fixture/API response with all six variables and five sources -> UI displays unit, bbox, resolution, cycle, valid time, and restricted reason without generated values.
- Geospatial contract: station and grid fixtures with lon/lat/bbox -> markers and query popup show contract coordinates and do not show stale popup data after source/station changes.
- Time-series contract: empty valid times, stale selected valid time, and partial station series -> timeline disables or resets visibly; charts mark missing/QC intervals.
- Resource/error contract: failed tile, unsupported comparison, over-limit area stat, missing forcing -> scoped unavailable/error states replace stale tiles/charts.
- Station resource contract: inventory page/search limit and series time-range/sample limit inputs -> bounded list/chart rendering or explicit empty/truncated/validation state.
- Dependency compatibility: no dependency change -> record existing utilities used; dependency added -> rationale, lockfile diff, and install/type/build/test evidence.
- Legacy compatibility: M11 meteorology placeholders and segment detail station/forcing behavior remain unchanged unless they link to the new route.

## Non-Goals / Explicit Exclusions

- Do not enable CLDAS credentials or prove live CLDAS download.
- Do not fabricate meteorology raster values, station rows, or forcing series to make the UI look populated.
- Do not implement production national raster publishing or hydrology MVT performance work.
- Do not add bias correction, downscaling, assimilation, or multi-source fusion beyond contract-provided comparison/provenance display.
