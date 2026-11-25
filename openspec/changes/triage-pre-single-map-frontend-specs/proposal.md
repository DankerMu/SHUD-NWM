# Triage the frontend capability specs written before the single-map UI (#3243)

Fixture level: standard. Risk packs: none (spec text, one governance doc,
tests only). Review seats: one (spec against code, test evidence).

## Why

The display frontend is one full-screen map page at `/` with floating
controls and two curve windows. Five capability specs still describe the
UI before that redesign: pages that are now redirects, panels and a basin
tree that do not exist, colours and legends the chart does not draw, an
analysis curve the river window never requests. One behaviour that does
exist, the per-source degraded display of the river curve window, is
stated by no spec, and one scenario states the opposite.

The frontend dead-code cleanup (#3241) decided the coupling this issue
names: the issue-time divider and the legacy colours were deleted, so the
rewritten text must not prescribe them.

## Verdict per requirement

Evidence re-read on the tree after #3241 (`76ad30207`). Paths are under
`apps/frontend/src/`. "Owner" is the current spec that already states the
behaviour.

### `forecast-frontend` (retired as a capability)

| Requirement | Verdict | What happens to it |
|---|---|---|
| Map initialization | Stale: `/forecast` is a redirect (`App.tsx:61`); the map opens on China bounds (`components/map/m11MapRuntime.tsx:38-44`) | Removed. Extent: `national-overview-page` "Overview map displays national basin context". Zoom control per form: `mobile-map-overlay-layout` "Map zoom controls are hidden in mobile form", `mobile-viewport-shell`. Scale bar and attribution: dropped as a stated requirement (they exist, `components/map/M11MapLibreSurface.tsx:407-410`) |
| River network layer | Stale: a vector-tile branch exists (`components/map/m11MapBuilders.ts:35,147-152`); no `stream_order` anywhere, width and colour follow `Type` or the value | Removed. Owner: `frontend-mvt-layer-consumption`, `hydromet-multibasin-display` (which retires `stream_order`) |
| Segment hover interaction | Highlight duplicated by `m11-river-hover-highlight`; no tooltip exists (`pages/OverviewPage.tsx:630-639` only prefetches) | Removed. Owner: `m11-river-hover-highlight`. Tooltip and pointer cursor: dropped as stated requirements |
| Segment click interaction | Superseded. Clicking empty map does not close the window: `onMapClick` only folds the launchers (`pages/OverviewPage.tsx:298`); the window closes by its close button, a layer change, or the one-window rule in mobile form | Removed. Owner: `map-feature-popups` (river window), `mobile-curve-sheet` |
| Forecast chart display | Stale: the only consumer renders the compact variant, no title, no echarts legend (`components/charts/ForecastChart.tsx:228-240`); colours are fixed per source | Removed. Owner: `multi-source-comparison-ui`, rewritten below (axes and tooltip carried there) |
| Chart-API integration | Stale: the query carries the full identity, one scenario and `include_analysis=false` (`lib/hydroMet/riverForecast.ts:120-128`), one request per source; the error string and `request_id` are not rendered | Removed. Owner: `map-feature-popups` (river window), `hydromet-multibasin-display` (strict identity) |
| Loading and error states | Stale: none of the three texts exists; no retry control | Removed. Owner: `map-feature-popups` (river window, rewritten below); map-source failure: `national-overview-page` (rewritten below) |

### `national-overview-page`

| Requirement | Verdict | What happens to it |
|---|---|---|
| National overview is the default operational entry | Stale; contradicts `single-map-shell-routing` (no top navigation). No side panels, one breakpoint pair (`hooks/useMobileForm.ts:12`) | Removed. Owner: `single-map-shell-routing`, `map-first-layout-conformance`, `mobile-viewport-shell` |
| Basin tree controls national map visibility | Stale: no tree, no checkbox; every basin is always visible | Removed, dropped |
| Overview map displays national basin context | Keep | Unchanged |
| Right-side summaries provide operational links | Stale: no summary card; the only ops entry is `M11OpsLink` | Removed, dropped. Ops entry: `single-map-shell-routing` |
| Overview handles degraded data states | Partly stale: the empty state is a floating notice, not a left panel; no basin row, no summary card, no retry control | Removed; replaced by "Overview reports missing data in floating notices" |
| Header brand identity for V2.0 | Keep | Unchanged |
| The basemap-unavailable status SHALL NOT be cleared by overview data settling | Keep (`components/map/m11MapRuntime.tsx:282-312`) | Unchanged |

### `multi-source-comparison-ui`

| Requirement | Verdict | What happens to it |
|---|---|---|
| 多源曲线渲染 | Stale: no analysis series; GFS `#22d3ee`, IFS `#34d399` (`components/map/M11RiverForecastPanel.tsx:36`); HTML legend strip; newline-joined tooltip; a missing source keeps its legend item | Removed; replaced by "GFS 与 IFS 预报曲线同图渲染" |
| IFS 06/18 周期可用时效标注 | Keep | Unchanged |
| 起报时刻信息增强 | Stale: one issue-time selector for both sources, no per-source label | Removed. Owner: `map-feature-popups` (the issue-time selector requirements) |
| API Response 多源元数据 | Backend; out of scope | Unchanged |

### `analysis-forecast-curve-splicing`

| Requirement | Verdict | What happens to it |
|---|---|---|
| Frontend Curve Splicing Display | Stale: the river window sends `include_analysis=false` and a non-forecast scenario fails validation (`lib/hydroMet/riverForecast.ts:127,302-304`); the divider was deleted in #3241; the compact chart shows no source annotation | Removed, dropped |
| Issue Time Display | Stale (same divider; not in the issue's table, found in the re-read) | Removed, dropped |
| The backend requirements | Not frontend | Unchanged |

### `map-feature-popups`

| Requirement | Verdict | What happens to it |
|---|---|---|
| 点击河段要素弹出 q_down 预报曲线 | Partly stale: scenario "身份不符不画曲线" blanks the window on any `ok:false`; the window degrades per source | Removed; replaced by "点击河段要素弹出按源独立加载的 q_down 预报曲线窗", which carries the per-source degraded display (issue section 2) |
| popup 源解析与 honest-display 不变量 | Stale for both windows: each always loads GFS and IFS (`components/map/M11RiverForecastPanel.tsx:34,251`, `components/map/M11StationForcingPopup.tsx:63,167`); no `best` request exists; `productReady` / `availability.ready` have no hit in non-test source | Removed. Source choice: "Curve windows SHALL NOT offer a forecast-source switch". Strict identity: the river and station requirements. The one live remnant, the station window's waiting state, becomes "代站曲线窗在源未解析时不发请求" |

## What changes

- Spec deltas for the five capabilities as the tables say. `forecast-frontend`
  loses every requirement and is retired (`retire_capabilities: true`).
- Purposes written by hand after the archive (a delta cannot carry one):
  `national-overview-page`, `analysis-forecast-curve-splicing` (both `TBD`),
  and `multi-source-comparison-ui` (drops "issue-time information").
- Tests only where an added scenario has no test that could go red.
- `docs/governance/LEGACY_DEAD_CODE_INVENTORY.md`: the #410 entry records
  that #432 (`1710375b1`) deleted the modules it lists as pending; the
  sentence and the inventory row that class `components/flood` as live are
  marked historical.

## Decisions

- **D1 Triage is per scenario.** A true scenario inside a stale requirement
  is either stated by a named owner or said to be dropped; none is lost
  silently.
- **D2 Normative text names files, not lines.** Line numbers are evidence
  in this proposal only.
- **D3 Incidental behaviour gets no SHALL.** Not specified: the 600 ms
  delay before the loading text, the pending placeholder, the `暂无
  latest-product` reason (the fetch throws instead of returning nothing),
  and which source's cycle labels the chart when no issue time is selected
  (the first source that succeeded).
- **D4 A rewritten requirement is REMOVED plus ADDED under a new title.**
  `openspec` refuses a MODIFIED requirement that drops a scenario.
- **D5 Every added scenario has a test that can go red.** The issue rules
  out code changes; tests for the scenarios this change adds are the one
  exception, and no production line changes.

## Not in this change

- Any production code. Where spec and code disagree, the spec follows.
- `multi-source-comparison-ui` "API Response 多源元数据" (backend).
- `hydro-met-mvp-ui` "Station and river chart interactions" also speaks of
  station rows and a right-side chart area; it is not in the issue and is
  reported as a finding.
- Findings reported, not specified (D3): when no issue time is selected,
  GFS and IFS each resolve their own latest cycle and the chart is
  labelled with the first successful source's; no test covers it.
