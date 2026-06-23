## 1. Dark Issue-Time Selector

- [x] 1.1 Add a shared M11 dark issue-time selector wrapper around the existing frontend Select primitive, preserving accessible labels, disabled states, and popup-theme styling.
- [x] 1.2 Replace native issue-time selects in river forecast, station forcing, and shared popup source controls with the dark selector while preserving cycle-selection behavior and useful test ids.
- [x] 1.3 Update popup unit tests to cover dark selector rendering, issue-time changes, disabled retained-window options, and no regression in GFS/IFS reload behavior.

Issue #658 evidence rows:

- River cycle list input:
  `["2026-05-21T00:00:00Z","2026-05-20T12:00:00Z","2026-05-20T00:00:00Z"]`
  -> opening the issue-time trigger exposes dark selector content, selecting
  `2026-05-20T12:00:00Z` reloads both `GFS` and `IFS` with that exact cycle,
  and the river panel keeps the existing useful test id.
- River retained-window input: selected cycle `2026-05-20T12:00:00Z` while the
  backend returns latest `2026-05-21T00:00:00Z` -> the UI shows the unavailable
  retained-window reason and does not draw stale q_down data.
- Station retained-window input: `DEFAULT_CYCLE` plus `RETAINED_OUT_CYCLE` ->
  opening the issue-time trigger exposes the same dark selector content,
  selecting the retained-out cycle reloads both station sources, keeps the
  available source plotted, and does not expose `Press` as chartable.
- Shared source controls input: `GFS` active, `IFS` alternative, one unavailable
  issue time -> source buttons keep their pressed state/callbacks,
  `m11-popup-issue-time` remains accessible, and unavailable issue-time items
  are visible but disabled/non-selectable.

## 2. Station Overlay State And Routing

- [x] 2.1 Add separate `metStations` query state parsing/serialization; normalize stale `layer=met-stations` URLs to a valid hydrology layer with station overlay enabled.
- [x] 2.2 Retire `met-stations` from the active hydrology layer contract in `M11Layer`, overview data contracts, fallback legends, legacy M11 controls, and tests; add verification that active code no longer serializes `layer=met-stations`.
- [x] 2.3 Update `/meteorology` legacy redirect semantics and `docs/spec/06_frontend_gis_design.md` to emit station overlay state instead of `layer=met-stations`, including tests for preserving existing query parameters and hydrology layer values.
- [x] 2.4 Update floating layer controls so hydrology layer selection remains single-choice and meteorological stations are controlled by an independent overlay toggle.
- [x] 2.5 Update overview and basin-detail station loading activation to depend on the overlay flag and visible/current basin contexts, preserving basin/model-scoped station inventory and honest empty/truncated states.
- [x] 2.6 Add station overlay loading tests for inactive overlay, unresolved source for station-series, missing basin contexts, paginated/truncated station inventory, and route-level status notes that appear only when `metStations=1`.

Issue #659 evidence rows:

- Query input `?layer=met-stations` -> parser returns `layer=discharge` and
  `metStations=true`; serializer returns `metStations=1` without
  `layer=met-stations`.
- Query input `?layer=flood-return-period&metStations=1` -> hydrology layer
  remains `flood-return-period` while station overlay is enabled.
- `/meteorology?source=IFS&validTime=2026-06-05T18:00:00Z` -> redirect target
  preserves source/validTime and appends `metStations=1`, not
  `layer=met-stations`.
- `/meteorology?layer=flood-return-period&source=IFS&validTime=2026-06-05T18:00:00Z`
  -> redirect target preserves the hydrology layer and source/validTime while
  enabling `metStations=1`.
- Floating controls: clicking hydrology choices dispatches hydrology `layer`
  patches only; clicking the station overlay toggle dispatches `metStations`
  patches only.
- Overview and basin-detail station loading: inactive overlay does not call
  `loadStationLayer`; enabled overlay with visible/current basin contexts does;
  unresolved `best`/`compare` does not block the inventory overlay request;
  missing basin contexts and truncated inventory render honest status notes only
  while the overlay is enabled; station-series popups still wait for concrete
  GFS/IFS before requesting curves.

## 3. Map Layer Ordering And Hit Behavior

- [x] 3.1 Render station cluster/point layers above hydrology layers when the overlay is enabled, while keeping discharge and other hydrology MVT layers registered and visible.
- [x] 3.2 Change map hover/click hit ordering so station clusters/points win on overlapped pixels, clusters expand on click, and exposed river lines still open river forecast.
- [x] 3.3 Update MapLibre surface tests for overlay render order, interactive layer ids, cluster expansion, station-over-river hit priority, and exposed-river clickability.

## 4. Draggable Coexisting Curve Windows

- [x] 4.1 Add a shared draggable curve-window frame or hook with header-only drag, viewport clamping, focus/z-index handling, and identity-based reset.
- [x] 4.2 Migrate river forecast and station forcing panels to the shared draggable frame, with sensible non-overlapping initial placement when both are visible.
- [x] 4.3 Remove mutual popup clearing in overview and basin-detail click handlers so river and station curve windows can coexist and close independently.
- [x] 4.4 Update component and route tests for river-then-station, station-then-river, independent close behavior, drag behavior, and chart/control interactions not starting drag.
- [x] 4.5 Add desktop and narrow-viewport bounding-box tests or Playwright checks proving both windows remain clamped in the map, close/header controls remain reachable, and dragging cannot push either window outside the viewport.

## 5. Verification

- [x] 5.1 Run targeted frontend tests for query state, overview data contracts, station layer store/hook, floating controls, MapLibre surface, river forecast panel, station forcing popup, and M11 routes.
- [x] 5.2 Run `cd apps/frontend && corepack pnpm test` and `cd apps/frontend && corepack pnpm build`.
- [x] 5.3 Run `openspec validate m11-popup-station-overlay-usability --strict --no-interactive`.
- [ ] 5.4 After deployment to node-27, capture live display evidence that `/`, `/meteorology`, river click, station click, dual-window comparison, drag movement, and dark issue-time selector behavior work against `https://test.nwm.ac.cn/`.
