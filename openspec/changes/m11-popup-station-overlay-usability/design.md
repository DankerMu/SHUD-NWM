## Context

The current single-map display already has separate state for a river forecast
panel and a station forcing panel, but the click handlers clear the other panel
when one opens. Both panels are centered absolute overlays with the same
`z-index` and no drag affordance, so opening both would overlap even if the
mutual clearing were removed.

Meteorological stations are still represented as `M11Layer = 'met-stations'`.
That makes station rendering mutually exclusive with the active hydrology layer
and causes `/meteorology` to encode `layer=met-stations`. The older GIS design
uses independent layer toggles and the customer-facing MVP description expects
one map where users can click both river segments and forcing stations.

The forecast cycle controls inside the dark glass popups are native `select`
elements. Their controls are styled dark, but the browser/OS renders native
option popups independently; on some devices the option surface becomes white
with low-contrast text.

## Goals / Non-Goals

**Goals:**

- Keep river q_down and station forcing comparison in one map workflow.
- Preserve shareable URL state while retiring `layer=met-stations` as the
  primary layer representation.
- Make station points/clusters visually and interactively sit above hydrology
  lines without making rivers impossible to click.
- Make curve windows movable and independently closable so users can compare a
  station forcing curve with a river flow curve.
- Make all issue-time selectors in M11 curve popups use a consistent dark
  popup surface across supported browsers.

**Non-Goals:**

- No backend API changes.
- No station-MVT endpoint implementation.
- No meteorological raster/grid layer restoration.
- No change to q_down forecast-series or station-series loading contracts.
- No new multi-station or multi-river comparison chart model.

## Decisions

### Use a controlled dark selector for issue-time menus

M11 popup issue-time controls should use the existing `components/ui/select.tsx`
Radix wrapper, or a thin M11-specific wrapper around it, instead of relying on
native `option` styling. The trigger and content must be styled for the dark
glass popup context, preserve accessible labels, preserve existing test ids
where practical, and support disabled retained-window options.

Alternative considered: add inline styles to native `<option>` elements. That is
smaller but remains dependent on browser/OS native menu behavior and is the
root of the current inconsistent white surface.

### Split station overlay state from hydrology layer state

`layer` should continue to represent the active hydrology product layer such as
`discharge`, `flood-return-period`, or `warning-level`. Station visibility should
be encoded as a separate boolean query state, serialized as `metStations=1`.

Legacy compatibility rules:

- `?layer=met-stations` is accepted as a stale alias and normalized to
  `layer=discharge&metStations=1`.
- `/meteorology` redirects to `/` with `metStations=1`; it does not create new
  `layer=met-stations` URLs.
- If a legacy URL also carries a valid hydrology layer, the valid hydrology
  layer remains active and station overlay is enabled.

Alternative considered: keep `met-stations` in `M11Layer` and introduce a
special case where that layer also draws discharge. That would preserve the
current enum but keeps the misleading mental model and makes timeline, legend,
and MVT source selection harder to reason about.

### Treat stations as an overlay in rendering and hit testing

When `metStations` is enabled and station features are available, the MapLibre
station source/layers render after hydrology layers. Station point and cluster
hit testing runs before river hit testing for overlapping pixels. Exposed river
line pixels still open the river forecast panel.

Station inventory loading uses the existing basin/model-scoped station API:
overview mode derives station requests from visible basin contexts, and
basin-detail mode uses the current basin context. Source/cycle identity remains
strict for the station-series curve request after a station is clicked; it is
not a station-inventory filter. Empty or truncated station inventory results
remain honest UI states.

### Use a shared draggable curve-window frame

River and station panels should share a draggable popup frame or hook. Dragging
starts from the panel header/handle only, not from the chart body, so chart
zoom/tooltip interactions remain intact. The frame clamps movement to the map
viewport, tracks focus so the active window rises above the other window, and
resets to a sensible default position when the selected feature identity changes.

Initial placement should avoid perfect overlap when both panels are open, for
example river left-of-center and station right-of-center on desktop, while still
falling back to centered/clamped placement on narrower viewports.

Alternative considered: keep one centered modal and require users to close it
before selecting another feature. That directly conflicts with the requested
same-screen comparison workflow.

## Risks / Trade-offs

- URL migration risk -> cover stale `layer=met-stations`, `/meteorology`, and
  new `metStations=1` parsing/serialization in unit and route tests.
- Hit-priority risk -> explicitly test station-over-river overlap and river
  clickability on exposed line pixels.
- Draggable layout risk -> clamp to the map viewport and test desktop plus
  narrow viewports so axes, close buttons, and charts remain usable.
- Selector regression risk -> keep keyboard-accessible labels and disabled
  option semantics while changing the implementation from native select to a
  controlled select.

## Issue #658 Fixture: Dark Issue-Time Selectors

Fixture level: expanded
Repair intensity: medium
Expanded trigger rationale:

- NHMS profile mandatory triggers apply because the selector preserves
  `forecast window`, `GFS`, `IFS`, and `forcing` issue-time reload behavior.
Change surface:

- `M11PopupChrome`, river forecast panel, station forcing popup, and popup
  component tests.

Must preserve:

- Existing issue-time/source selection callbacks still reload the same GFS/IFS
  series for the selected feature identity.
- River q_down, station PRCP/TEMP/RH/wind/Rn, and existing empty/partial states
  remain downstream consumers of the same validated series contracts.
- Disabled retained-window issue-time choices remain visible but
  non-selectable.
- Popup labels, useful test ids, and keyboard-accessible control semantics
  remain available after replacing native selects.

Must add/change:

- M11 popup issue-time controls use a controlled dark selector surface instead
  of native `option` popups.
- Shared popup source controls, where issue-time-adjacent native selects appear
  in the curve-window chrome, use the same dark popup control treatment.

Risk packs considered:

- Public API / CLI / script entry: not selected - frontend component-only
  change, no exported API or route contract changes.
- Config / project setup: not selected - no build or environment config change.
- File IO / path safety / overwrite: not selected - no file system surface.
- Schema / columns / units / field names: not selected - no data schema or unit
  change.
- Auth / permissions / secrets: not selected - no auth boundary touched.
- Concurrency / shared state / ordering: not selected - no async ordering model
  change beyond existing selection callbacks.
- Resource limits / large input / discovery: not selected - no discovery or
  unbounded input surface.
- Legacy compatibility / examples: selected - preserve test ids, accessible
  labels, disabled retained-window options, and existing cycle-selection
  behavior.
- Error handling / rollback / partial outputs: selected - unavailable retained
  cycles must stay non-selectable and honest.
- Release / packaging / dependency compatibility: not selected - uses existing
  frontend Select primitive, no new package.
- Documentation / migration notes: not selected - behavior is covered by this
  OpenSpec and no user-facing migration is required.

Domain packs:

- Geospatial / CRS / basin geometry: not selected - no map geometry, CRS, or
  basin-selection behavior changes.
- Hydro-met time series / forcing windows: selected - station forcing and river
  q_down issue-time changes must continue to reload the intended GFS/IFS
  retained windows.
- SHUD numerical runtime / conservation / NaN: not selected - no SHUD runtime
  or numerical output semantics change.
- PostGIS / TimescaleDB domain behavior: not selected - no database query or
  persisted state surface.
- Slurm production lifecycle / mock-vs-real parity: not selected - no scheduler
  or compute lifecycle surface.
- External hydro-met providers / snapshot reproducibility: not selected - this
  PR only changes already-loaded frontend cycle selection controls.
- Run manifest / QC provenance: not selected - no manifest or QC evidence
  surface.
- Published NHMS artifacts / display identity: selected - selector changes must
  not alter station/river identity used by existing curve reloads.

Required evidence:

- River popup test with cycles
  `2026-05-21T00:00:00Z`, `2026-05-20T12:00:00Z`,
  `2026-05-20T00:00:00Z`: opening the issue-time trigger shows a dark
  selector surface, selecting `2026-05-20T12:00:00Z` reloads both `GFS` and
  `IFS` with that cycle, and the existing panel test id remains queryable.
- River retained-window test: when the user selects
  `2026-05-20T12:00:00Z` but the backend returns the latest cycle, the option
  state remains honest and the panel shows the existing unavailable reason
  rather than drawing a stale curve.
- Station popup test with `DEFAULT_CYCLE` and `RETAINED_OUT_CYCLE`: opening the
  issue-time trigger shows the same dark selector surface, selecting the
  retained-out cycle calls station-series loads for both `GFS` and `IFS`, and
  the unavailable source stays visible without turning `Press` into an
  available variable.
- Source controls test: `M11PopupSourceControls` preserves `GFS`/`IFS` source
  buttons, `m11-popup-issue-time` identity, accessible labels, and disabled
  retained-window options while using the controlled dark selector content.
- `cd apps/frontend && corepack pnpm test -- M11RiverForecastPanel M11StationForcingPopup`
- `cd apps/frontend && corepack pnpm build`
- `openspec validate m11-popup-station-overlay-usability --strict --no-interactive`

Non-goals:

- No query-state migration, station overlay render-order change, draggable
  window frame, backend API change, or new station variable support.

## Issue #659 Fixture: Station Overlay Query State And Routing

Fixture level: expanded
Repair intensity: high
Expanded trigger rationale:

- This slice changes shareable URL state, legacy route semantics, active
  frontend layer contracts, and station-overlay loading activation.
- A partial migration would either keep emitting retired `layer=met-stations`
  URLs or accidentally make station inventory requests while the overlay is
  disabled.

Change surface:

- `apps/frontend/src/lib/m11/queryState.ts`
- `apps/frontend/src/components/map/M11FloatingControls.tsx`
- `apps/frontend/src/components/map/M11MapLibreSurface.tsx`
- `apps/frontend/src/pages/OverviewPage.tsx`
- `apps/frontend/src/components/m11/BasinDetailPanels.tsx`
- `apps/frontend/src/pages/m11/M11Controls.tsx`
- `apps/frontend/src/pages/m11/useStationLayer.ts`
- `apps/frontend/src/stores/overviewData.ts`
- `apps/frontend/src/lib/m11/overviewDataContracts.ts`
- `apps/frontend/src/App.tsx`
- Frontend unit/component route tests and mocked M11 route e2e expectations.

Must preserve:

- `layer` continues to choose only hydrology product layers:
  `discharge`, `flood-return-period`, and `warning-level`.
- Hydrology layer state remains serialized and shareable independently of
  station overlay visibility.
- Source/cycle strictness remains required for station-series curve requests
  after a station click; station inventory loading must not claim source/cycle
  filtering.
- When `metStations=1` and valid basin contexts exist, station inventory
  loading still runs from basin/model scope even if `source=best` or
  `source=compare` has not resolved to concrete GFS/IFS; unresolved source
  honesty belongs to the station-series curve after a station click.
- Existing basin/model-scoped station inventory loading, pagination cap,
  truncation honesty, and unresolved-source honesty remain intact.
- Stale links and old route entry points continue to land on a usable map.

Must add/change:

- Add `metStations` boolean query state, serialized as `metStations=1` only
  when enabled and omitted when disabled.
- Parse stale `layer=met-stations` as `layer=discharge` plus
  `metStations=true`; serialized canonical URLs must not contain
  `layer=met-stations`.
- Redirect `/meteorology` with `metStations=1` and preserve existing valid
  hydrology `layer` values from the original query.
- Replace the exclusive "流量 / 气象代站" layer choice with hydrology selection
  plus an independent station overlay toggle.
- Activate station overlay rendering/loading/status notes from `metStations`
  rather than `state.layer`.

Risk packs considered:

- Public API / CLI / script entry: selected - URL query parameters and legacy
  routes are user-facing contracts.
- Config / project setup: not selected - no build or environment config
  changes.
- File IO / path safety / overwrite: not selected - no file system surface.
- Schema / columns / units / field names: selected - `M11Layer` type and
  overview layer labels/fallback legends are narrowed.
- Auth / permissions / secrets: not selected - no auth boundary touched.
- Concurrency / shared state / ordering: selected - URL replacement effects and
  station-store fetch effects must not loop or keep stale overlay data.
- Resource limits / large input / discovery: selected - station inventory
  pagination/truncation cap must remain honest.
- Legacy compatibility / examples: selected - `/meteorology` and
  `layer=met-stations` links must continue to work.
- Error handling / rollback / partial outputs: selected - inactive overlay,
  unresolved source, missing basin contexts, and truncated inventory require
  explicit honest states.
- Release / packaging / dependency compatibility: not selected - no new
  package.
- Documentation / migration notes: selected - source GIS design doc must match
  the new route/query semantics.

Domain packs:

- Geospatial / CRS / basin geometry: selected - station overlay feature
  rendering remains map/basin-context scoped, though CRS is unchanged.
- Hydro-met time series / forcing windows: selected - station-series popup
  source/cycle strictness must not be weakened by inventory-overlay changes.
- SHUD numerical runtime / conservation / NaN: not selected - no model runtime
  or numerical output change.
- PostGIS / TimescaleDB domain behavior: not selected - no database query
  contract changes.
- Slurm production lifecycle / mock-vs-real parity: not selected - no scheduler
  or compute lifecycle surface.
- External hydro-met providers / snapshot reproducibility: selected -
  GFS/IFS/best/compare source resolution controls station-series identity.
- Run manifest / QC provenance: not selected - no manifest or QC evidence
  surface.
- Published NHMS artifacts / display identity: selected - station and river
  popup identities must stay source/basin/segment/station truthful.

Required evidence:

- Query-state tests show `metStations=1` parses/serializes as an enabled
  overlay, disabled/default state omits it, stale `layer=met-stations`
  normalizes to `layer=discharge&metStations=1`, and active serialization never
  emits `layer=met-stations`.
- Route tests show `/meteorology` redirects to `/?metStations=1`, preserves
  original source/validTime parameters, and preserves an existing valid
  hydrology `layer=flood-return-period` while enabling the station overlay.
- Floating-control tests show hydrology layer selection dispatches only
  hydrology `layer` values and the station toggle dispatches
  `{ metStations: true/false }`.
- Overview and basin-detail tests show station inventory loading occurs only
  when `metStations=1` and valid basin contexts exist; inactive overlay does
  not load, `best`/`compare` unresolved source does not block the inventory
  overlay request, station-series popups still require concrete GFS/IFS before
  requesting curves, and status notes appear only while the overlay is enabled.
- Store/hook tests continue to cover pagination/truncation and missing basin
  version honesty without an unbounded all-stations request.
- `rg "layer=met-stations" apps/frontend/src apps/frontend/e2e` after the
  implementation returns only legacy/stale test inputs or comments that
  explicitly describe normalization, not active serializer/control output.
- `cd apps/frontend && corepack pnpm test -- queryState M11FloatingControls M11Shell AppRoutes stationLayerData m11OverviewDataContracts`
- `cd apps/frontend && corepack pnpm test`
- `cd apps/frontend && corepack pnpm build`
- `openspec validate m11-popup-station-overlay-usability --strict --no-interactive`

Non-goals:

- No MapLibre render-order/hit-priority changes beyond wiring station overlay
  visibility to `metStations`; issue #660 owns hit-priority ordering.
- No draggable or coexisting curve-window behavior; issue #661 owns that work.
- No backend API or station-MVT endpoint change.

## Issue #660 Fixture: Station Overlay Layer Ordering And Hit Priority

Fixture level: expanded
Repair intensity: medium
Expanded trigger rationale:

- This slice changes MapLibre render order and interaction dispatch order for
  overlapping station and hydrology features.
- A regression would either hide/disable hydrology while the station overlay is
  enabled, or make station-over-river pixels open the wrong workflow.

Change surface:

- `apps/frontend/src/components/map/M11MapLibreSurface.tsx`
- `apps/frontend/src/pages/__tests__/M11Shell.test.tsx`

Must preserve:

- Active hydrology MVT/GeoJSON river layers remain registered and visible while
  station overlay source/layers are enabled.
- Exposed river line pixels still dispatch the river forecast workflow.
- Cluster clicks expand/fly to the cluster and do not open station forcing
  popups.
- Station inventory loading and route/query state remain owned by issue #659.

Must add/change:

- Station cluster/point layers render above active hydrology layers.
- `interactiveLayerIds` includes `met-stations-point` and `clusters` ahead of
  hydrology hit layers when station features are renderable.
- Hover/click dispatch checks station point/cluster hits before hydrology
  river hits for overlapped pixels.

Risk packs considered:

- Public API / CLI / script entry: not selected - no route, query, or external
  API contract changes.
- Config / project setup: not selected - no build or environment config
  changes.
- File IO / path safety / overwrite: not selected - no file system surface.
- Schema / columns / units / field names: not selected - no data schema or
  property contract change.
- Auth / permissions / secrets: not selected - no auth boundary touched.
- Concurrency / shared state / ordering: selected - MapLibre feature ordering
  and dispatch precedence are the core behavior.
- Resource limits / large input / discovery: not selected - station inventory
  pagination/caps are unchanged.
- Legacy compatibility / examples: selected - existing river click and cluster
  expansion behavior must remain compatible.
- Error handling / rollback / partial outputs: selected - cluster expansion
  must fail closed without opening an incorrect popup.
- Release / packaging / dependency compatibility: not selected - no dependency
  or packaging change.
- Documentation / migration notes: selected - fixture records the split from
  #659 query-state work and #661 draggable-window work.

Domain packs:

- Geospatial / CRS / basin geometry: selected - map layer order and feature hit
  testing are geospatial UI behavior, though CRS and geometry contracts are
  unchanged.
- Hydro-met time series / forcing windows: selected - dispatch must preserve
  station forcing versus river q_down workflow identity.
- SHUD numerical runtime / conservation / NaN: not selected - no model runtime
  or numerical output change.
- PostGIS / TimescaleDB domain behavior: not selected - no database query
  contract changes.
- Slurm production lifecycle / mock-vs-real parity: not selected - no scheduler
  or compute lifecycle surface.
- External hydro-met providers / snapshot reproducibility: not selected - no
  provider source/cycle semantics change.
- Run manifest / QC provenance: not selected - no manifest or QC evidence
  surface.
- Published NHMS artifacts / display identity: selected - station and river
  popup identities must remain truthful under overlapped map hits.

Required evidence:

- MapLibre surface tests show hydrology MVT remains registered while station
  clustered-GeoJSON source/layers are also registered.
- Tests assert station cluster/point layers render after the active hydrology
  line/hit layers and interactive layer ids include station ids before
  hydrology ids.
- Tests assert cluster clicks call `getClusterExpansionZoom`/`flyTo` and do not
  dispatch station popup opening.
- Tests assert station-over-river hover/click prioritizes the station feature.
- Tests assert exposed river pixels remain clickable with station overlay
  enabled.
- `cd apps/frontend && corepack pnpm test -- M11Shell AppRoutes`
- `cd apps/frontend && corepack pnpm build`
- `openspec validate m11-popup-station-overlay-usability --strict --no-interactive`

## Issue #661 Fixture: Draggable Coexisting Curve Windows

Fixture level: expanded
Repair intensity: medium
Expanded trigger rationale:

- This slice changes shared frontend window state, pointer interaction, and
  side-by-side river/station comparison behavior.
- A regression could make chart zoom/tabs/selectors start dragging, hide the
  close controls off viewport, or reintroduce river/station mutual exclusion.

Change surface:

- `apps/frontend/src/components/map/M11DraggableCurveWindow.tsx`
- `apps/frontend/src/components/map/M11RiverForecastPanel.tsx`
- `apps/frontend/src/components/map/M11StationForcingPopup.tsx`
- `apps/frontend/src/pages/OverviewPage.tsx`
- `apps/frontend/src/components/m11/BasinDetailPanels.tsx`
- Focused component, route, shell, and mocked display tests.

Must preserve:

- River q_down and station forcing data loading, strict identity validation,
  honest empty/partial states, and dark issue-time selector behavior remain
  unchanged.
- Station overlay query state and MapLibre hit priority remain owned by issues
  #659 and #660; this issue only consumes their click dispatch results.
- Basin-detail station overlays keep resolved detail identity first; the
  current query basin version is only a fallback after same-basin detail state
  has settled without a resolved selected version, so stale resolved identities
  still win over URL drift. Overview station overlays may likewise fall back to
  the query basin version only when overview metadata maps that version back to
  the same basin.
- Chart body, ECharts data zoom, station variable tabs, source/cycle controls,
  and close buttons keep their native control behavior.

Must add/change:

- River and station panels use one shared draggable curve-window frame or hook.
- Drag starts only from the header/handle and clamps within the map viewport.
- River and station windows can coexist, close independently, and initially use
  non-identical desktop placements with clamped narrow-viewport fallback.
- Click, focus, or drag raises the active window above the other.
- Selecting a different river or station resets only that window's placement;
  the other visible window keeps its feature identity and position.

Risk packs considered:

- Public API / CLI / script entry: not selected - no route or API contract
  change.
- Config / project setup: not selected - no build or environment config
  change.
- File IO / path safety / overwrite: not selected - no filesystem surface.
- Schema / columns / units / field names: not selected - no data schema change.
- Auth / permissions / secrets: not selected - no auth boundary touched.
- Concurrency / shared state / ordering: selected - two independent windows
  share z-index, focus, pointer-drag, and identity-reset behavior.
- Resource limits / large input / discovery: not selected - no new data fetch
  scope or unbounded input.
- Legacy compatibility / examples: selected - existing single-window tests and
  route workflows must keep working while allowing coexistence.
- Error handling / rollback / partial outputs: selected - close/clamp/focus
  paths must fail without hiding the remaining window.
- Release / packaging / dependency compatibility: not selected - no new
  package.
- Documentation / migration notes: selected - fixture records the split from
  prior query-state and hit-priority issues.

Domain packs:

- Geospatial / CRS / basin geometry: selected - windows are clamped to the map
  viewport and triggered by map feature identities, though CRS is unchanged.
- Hydro-met time series / forcing windows: selected - river q_down and station
  forcing curve identities must remain truthful while windows coexist.
- SHUD numerical runtime / conservation / NaN: not selected - no runtime or
  numerical output change.
- PostGIS / TimescaleDB domain behavior: not selected - no database behavior
  change.
- Slurm production lifecycle / mock-vs-real parity: not selected - no scheduler
  or compute lifecycle surface.
- External hydro-met providers / snapshot reproducibility: not selected -
  source/cycle provider semantics are unchanged.
- Run manifest / QC provenance: not selected - no manifest or QC evidence
  surface.
- Published NHMS artifacts / display identity: selected - selected river and
  station identities must remain independent and honest across close/reset
  behavior.

Required evidence:

- Component tests prove header-only drag, chart/control interactions do not
  drag, desktop and narrow viewport clamp behavior, non-zero container
  bounding-box reachability for the drag handle and close button, active
  z-index props, and identity-based position reset for river and station
  windows.
- Route mocked tests prove overview and basin-detail river-then-station and
  station-then-river workflows leave both windows visible, closing one does not
  close the other, default positions are not identical on desktop, and clicking
  either window raises it.
- Existing issue-time selector and curve-loading tests continue to pass for
  river and station panels.
- `cd apps/frontend && corepack pnpm test -- M11RiverForecastPanel M11StationForcingPopup AppRoutes`
- `cd apps/frontend && corepack pnpm test`
- `cd apps/frontend && corepack pnpm build`
- `openspec validate m11-popup-station-overlay-usability --strict --no-interactive`

Non-goals:

- No backend API change, station-MVT endpoint change, route/query migration, or
  MapLibre hit-priority change.

## Migration Plan

1. Add the new query state and normalize old station-layer URLs.
2. Update controls and station loading to use the overlay boolean.
3. Update map render/hit ordering and panel coexistence.
4. Replace native issue-time selectors with the dark selector component.
5. Add/adjust unit, component, and mocked e2e coverage.
6. After merge/deploy, verify on node-27 display that `/`, `/meteorology`, river
   click, station click, and both-popups-open workflows still operate against
   live display data.

Rollback is local to frontend code: restoring the old query parser/control
behavior and disabling the station overlay toggle returns the map to the
previous exclusive layer model without backend migration.
