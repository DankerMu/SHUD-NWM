## Context

- `App.tsx` renders `<BrowserRouter><AppShell><Suspense fallback="加载中..."><AppRoutes/></Suspense></AppShell></BrowserRouter>`. `AppShell` renders `SiteHeader` and a `<main>` that holds the children.
- `OverviewPage.tsx` has a file-local `M11FullscreenMap` whose only caller is `OverviewMode`. It renders:
  - `M11MapLibreSurface`
  - `M11FloatingLayerSwitcher`, `M11FloatingBasemapSwitcher`, `M11OpsLink`
  - `{children}` (river forecast panel, station popup, floating notices)
  - the bottom control bar, when `controlBar` is present
  - `M11FloatingLegend`
- The page body computes `controlBar = useMemo(() => deriveM11ControlBarModel({...}))` and passes the model down.
  - `OverviewPageValidTimeCorrection.test.tsx` asserts the mount seam `m11-bottom-control-bar` synchronously on first render.
  - The comment next to that assertion notes that `deriveM11ControlBarModel` never returns null.
- `MonitoringPage.tsx` renders `SummaryBar` and a grid of `StageList`, `JobsTable` and `TrendPanel`. It serves both `/monitoring` and `/ops` (`mode`).

## Decisions

### D1: one boundary component, two usages

- Add `apps/frontend/src/components/layout/RegionErrorBoundary.tsx`, a class component. React still requires a class for boundaries, and no new dependency such as `react-error-boundary` is added.
- Props:
  - `region: string`: Chinese label, used in the log and in `aria-label`.
  - `testId: string`: the fallback gets `data-testid={testId}`.
  - `resetKeys?: readonly unknown[]`: when any element changes (`Object.is`), a caught error resets.
  - `variant?: 'region' | 'page'`
  - `className?: string`: positioning for floating regions.
  - `children`
- State `{ error: Error | null }`, set via `static getDerivedStateFromError`. `componentDidCatch(error, info)` calls `console.error('[RegionErrorBoundary]', region, error, info.componentStack)`. No remote reporting exists today and none is added.
- `componentDidUpdate(prevProps, prevState)` clears `error` only when `prevState.error !== null` AND `resetKeys` differ element-wise (react-error-boundary semantics). A key change in the same update that throws therefore keeps the fallback and does not trigger an immediate re-throw.
- Fallback:
  - `variant='region'`: a compact card with `role="alert"`, the text 「此区域加载失败」 and a 「重试」 button that clears `error` and re-renders the children.
  - `variant='page'`: 「页面加载失败」, 「重试」 (clear) and 「刷新页面」 (`window.location.reload()`).
  - The raw error message is not shown to users; it is logged only.
- Floating overlay regions pass a `className` so the fallback sits where the region was. The map surface fallback fills the map section.

### D2: route boundary

- A small `RouteErrorBoundary` wrapper, placed inside `BrowserRouter` (`AppShell` already is), reads `useLocation()` and renders `<RegionErrorBoundary variant="page" testId="route-error-fallback" region="页面" resetKeys={[location.pathname, location.search]}>`.
- Placement: export `AppFrame` = `<AppShell><RouteErrorBoundary><Suspense …><AppRoutes/></Suspense></RouteErrorBoundary></AppShell>` from `App.tsx`; `App` renders `<BrowserRouter><AppFrame/></BrowserRouter>`. Tests render `AppFrame` inside `MemoryRouter`, so the production composition itself is under test.
  - The boundary is **outside** `Suspense`, so a rejected lazy import is caught (React rethrows it to the nearest boundary).
  - `SiteHeader` lives in `AppShell`, outside the boundary, so the header and navigation stay usable.
  - Chunk failure: `React.lazy` caches a rejected import, so 「重试」 and returning to the same route re-throw. Only 「刷新页面」 (or navigating to a different route) recovers. This is accepted and documented; retry is not expected to fix a chunk failure.
- Effect on the Overview page: URL query changes (timeline steps, source changes) change `location.search`, so a page-level error resets on the next query change. This is intended, because a new query can recover.
  - The map region boundaries do **not** key on the full search. Their `resetKeys` are chosen per region (D3), so a crashing region is not re-mounted on every timeline tick.

### D3: overview regions and control-bar localization

- Inside `M11FullscreenMap`, wrap each region in its own boundary, with `resetKeys` in brackets:
  - `M11MapLibreSurface`: testId `region-error-map`, region 「地图」, `resetKeys: []`, fallback fills the section.
  - The three floating switchers together: `region-error-map-controls`, 「地图控件」, `[]`.
  - Forecast panels: in `OverviewMode`, one boundary around `riverForecastPanel` and `stationForecastPanel` only. testId `region-error-map-panels`, 「预报面板」, `resetKeys: [selectedSegmentId, selectedStationId]` (the local popup selection that reaches `M11FullscreenMap`), so selecting another segment or station retries. Its fallback carries a floating position class, because the panels are absolutely positioned overlays and a normal-flow card would sit under the map canvas. The floating notice chain (`m11-met-station-status` / `m11-overview-loading` / `m11-overview-empty` / `m11-precip-notice`) stays outside, so a panel crash never hides the `bootstrapError` surface.
  - Bottom control bar: `region-error-control-bar`, 「起报时次与时间轴」, `[state.source, state.cycle]`.
  - `M11FloatingLegend`: `region-error-legend`, 「图例」, `[]` (`state.layer` is always `'discharge'`, so it would never reset).
- The map fallback uses `absolute inset-0` inside the `relative` map section. Floating fallbacks take the region's existing position classes.
- Control-bar localization:
  - `M11FullscreenMap`'s prop changes from `controlBar?: M11BottomControlBarProps | null` to `controlBarInput?: M11ControlBarInput | null`. Null or absent still means no control-bar DOM.
  - A new file-local component `M11BottomControlBarRegion({ input, onQueryChange })` computes `useMemo(() => deriveM11ControlBarModel(input), [input])` and renders `<M11BottomControlBar {...model} onQueryChange={onQueryChange} />`.
  - `OverviewMode` memoizes the input object with the same dependencies the model memo had (`state`, `layers`, `cyclesBySource`, `sourceSelection`; `metadata` is still derived from `layers` inside the memo), so re-derivation frequency is unchanged.
  - `deriveM11ControlBarModel`, `M11BottomControlBar` and their props are unchanged. Every existing control-bar test keeps its assertions. The mount-seam test still sees `m11-bottom-control-bar` synchronously, because the child renders in the same commit.
- Why E4 is reachable and red pre-change (trace recorded by fixture review):
  - `deriveM11ControlBarModel` reads `cyclesBySource[resolveNationalScaleSource(state.source)]`, and `best` maps to `gfs`.
  - The store fetches `/cycles` for the concrete source on every load, including the default source, and stores the raw payload (`as T`).
  - Store-side consumers are null-safe: `pairRecord` uses `entry?.cycle_time`, and `dischargeCyclesDefaultCycle` reads only `default_cycle`.
  - The only render-time consumer is the control-bar memo in the `OverviewMode` body. So on the default `/`, a `gfs` payload with `cycles: [null, …]` and a valid `default_cycle` throws during render. Pre-change this is an uncaught render error; post-change it lands in the control-bar region.
  - `?source=ifs` without a valid `default_cycle` goes fail-closed (`cycles: []`) and does not throw, so it must not be used.

### D4: monitoring regions

In `MonitoringPage`, wrap each of `SummaryBar`, `StageList`, `JobsTable` and `TrendPanel` in its own boundary:

| Component | testId | Region |
|---|---|---|
| `SummaryBar` | `region-error-summary` | 「概要」 |
| `StageList` | `region-error-stages` | 「阶段」 |
| `JobsTable` | `region-error-jobs` | 「作业」 |
| `TrendPanel` (inside its grid wrapper div) | `region-error-trend` | 「趋势」 |

- `resetKeys`: `[visibleSource, visibleCycleTime]`.
- The grid layout is preserved:
  - `SummaryBar`, `StageList` and `JobsTable` have no placement class, so their boundaries wrap them directly.
  - For `TrendPanel`, the boundary goes **inside** the existing `<div className="min-[800px]:col-span-2 min-[1200px]:col-span-1">` grid item, so the col-span survives when the fallback shows.

### D5: what boundaries do not cover (recorded)

- Throws inside event handlers, `useEffect`, MapLibre event callbacks and async code are not caught by React boundaries.
- Throws in `OverviewMode`'s own body (other page-level derivations) reach the route boundary. The page shows 「页面加载失败」 with the header intact, not a blank screen.
- StrictMode double-invokes render in development only; there is no production effect.
