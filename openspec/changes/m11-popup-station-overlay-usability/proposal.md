## Why

The M11 display map now has the data needed for both river q_down curves and
forcing-station curves, but two frontend usability seams block the intended
side-by-side workflow: native cycle selectors can show unreadable white option
surfaces on some browsers, and meteorological stations are still modeled as an
exclusive main layer instead of an overlay on top of the hydrology map.

## What Changes

- Replace native forecast-cycle drop-down behavior in dark curve popups with a
  controlled dark selector surface that remains legible across supported
  browsers and devices.
- Change the meteorological station layer from an exclusive `layer=met-stations`
  mode to an independent station overlay that can be enabled while hydrology
  layers, especially `discharge`, remain visible and clickable.
- Render station points and clusters above hydrology lines; station/cluster hits
  take precedence where symbols overlap river lines, while exposed river lines
  remain clickable.
- Allow river forecast and station forcing curve windows to coexist, and make
  each curve window draggable so users can compare station forcing and river
  flow on the same map.
- Preserve legacy `/meteorology` and stale `layer=met-stations` links by
  normalizing them to the new station-overlay query state.

## Capabilities

### New Capabilities

- None. This change refines existing M11 display capabilities.

### Modified Capabilities

- `map-feature-popups`: add dark issue-time selector behavior and draggable,
  coexisting river/station curve windows.
- `met-station-cluster-layer`: change station rendering from exclusive main
  layer mode to a hydrology-compatible overlay, including hit priority and
  visible-basin loading semantics.
- `frontend-navigation-state`: encode station overlay state separately from the
  active hydrology layer and normalize stale query values.
- `single-map-shell-routing`: update legacy meteorology redirects to target the
  station overlay query state instead of the retired `layer=met-stations`
  primary-layer state.

## Impact

- Frontend query model and serialization:
  `apps/frontend/src/lib/m11/queryState.ts`
- Floating layer controls, legend behavior, and legacy route redirects:
  `apps/frontend/src/components/map/M11FloatingControls.tsx`,
  `apps/frontend/src/App.tsx`
- MapLibre station source/layer registration, render order, and click/hover hit
  ordering:
  `apps/frontend/src/components/map/M11MapLibreSurface.tsx`
- Overview and basin-detail popup state:
  `apps/frontend/src/pages/OverviewPage.tsx`,
  `apps/frontend/src/components/m11/BasinDetailPanels.tsx`
- River/station curve window chrome and selectors:
  `apps/frontend/src/components/map/M11PopupChrome.tsx`,
  `apps/frontend/src/components/map/M11RiverForecastPanel.tsx`,
  `apps/frontend/src/components/map/M11StationForcingPopup.tsx`
- Frontend tests under `apps/frontend/src/**/__tests__` and mocked route e2e
  coverage under `apps/frontend/e2e/m11-routes.mocked.spec.ts`.
- No backend API, database, station-MVT endpoint, or production compute behavior
  changes are in scope.
