# Deleted and re-homed tests (#3222)

Disposition is per test case (D3). Line numbers are on master f45e8f3a8 unless the path is a live file after this
change. "Live pin" names the test that pins the same behaviour on the path reachable from `src/main.tsx`; "dead" means
the behaviour existed only in deleted code.

Vitest count: 131 files / 1531 tests before, 130 files / 1516 tests after. Deleted cases: 11 in
`ForecastComparison.test.tsx` + 4 in `useHydroMetProduct.test.tsx` = 15; 1531 - 15 = 1516. Re-homed cases (5) keep their
count. Files: 131 - 2 deleted + 1 new (`ForecastChartComparison.test.tsx`) = 130.

## `apps/frontend/src/__tests__/ForecastComparison.test.tsx` (16 cases; file deleted)

| test (line) | what it asserted | disposition | live pin or reason |
| --- | --- | --- | --- |
| `toggles scenarios and keeps at least one selected` (:88) | `<ScenarioSelector/>` checkboxes: GFS checked and disabled while it is the only selection; `toggleScenario` adds IFS, removes GFS, refuses to remove the last one | deleted | Dead: selector and store only. The live river window has no source switch and always draws GFS + IFS: `M11RiverForecastPanel.test.tsx:204` "renders GFS + IFS on one axis in a side panel with wheel zoom, no source switch". |
| `passes selected scenarios in the forecast request` (:110) | `fetchForecast` sends `river_network_version_id: 'rn-1'`, `scenarios: 'GFS,IFS'`, `include_analysis: true` | deleted | Dead: one request with a comma-joined scenario list and analysis on is the store's shape. The live path sends one request per source with `scenarios=forecast_<src>_deterministic` and `include_analysis: false`; the full query is pinned by `toHaveBeenCalledWith` at `pages/hydroMet/__tests__/bootstrap.test.ts:804` (GFS) and `:847` (IFS). |
| `uses route handoff source and cycle options without changing default forecast behavior` (:132) | `fetchForecast({source:'ifs', issueTime})` sends that `issue_time` and `scenarios: 'IFS'` | deleted | Dead: route-handoff options are store API. Live: `issue_time` from the product cycle and the IFS scenario are pinned at `bootstrap.test.ts:804` / `:847`; reload on a new issue time at `M11RiverForecastPanel.test.tsx:360`. |
| `omits scenarios for source=best so the API can resolve best availability` (:163) | `source: 'best'` omits `scenarios` from the query | deleted | Dead: the live river path only knows `GFS` / `IFS` (`HydroMetSource`) and always sends `scenarios` (`riverForecast.ts:139`, pinned `bootstrap.test.ts:804`). |
| `rejects forecast responses for sibling segment identities` (:194) | store throws `预报曲线响应与请求河段不匹配` and leaves `forecastData` null when the response names another segment | deleted | Live behaviour differs in form: `validateHydroMetRiverForecastForChart` returns `ok: false` (no throw) and renders no points, pinned at `lib/hydroMet/__tests__/riverForecast.test.ts:135`; per source in the window at `M11RiverForecastPanel.test.tsx:349`. |
| `accepts ignored-context forecast responses even when a stale route context remains in the store` (:215) | `ignoreActiveRequestContext` request is accepted while a different `activeRequestContext` stays in the store | deleted | Dead: request-context binding (`activeRequestContext`, nonce identity) exists only in the store. |
| `persists restored source and cycle as the active forecast request context` (:265) | `setRequestContext` + `toggleScenario` + `useSelectedScenarios` produce the queries `GFS,IFS` then `GFS` and clear the context source | deleted | Dead: store-only state machine. |
| `caps oversized forecast payloads during normalization` (:302) | store normaliser keeps `FORECAST_CHART_POINT_BUDGET` (10 000) points and reports `pointBudgetStatus.overBudget` | deleted | Dead: the store normaliser and the only producer of `pointBudgetStatus`. The live path caps each series at 480 points (`riverForecast.ts:331-334`): **gap, no vitest pin** (D9). |
| `marks too many one-point series as over-budget and stops retaining extra series` (:341) | `takeSeries` stops at the series budget; `retainedSeriesCount` = budget, `overBudget` | deleted | Dead: `takeSeries` had the store as its only caller and is deleted (D8). The live analogue, the 8-series inspection limit (`riverForecast.ts:193-195`), is a **gap, no vitest pin** (new, not in D9). |
| `fails locally instead of issuing an unscoped forecast request` (:375) | empty `riverNetworkVersionId` throws `缺少 river_network_version_id` and issues no GET | deleted | Dead in this form. Live: `river_network_version_id` is taken from the latest-product identity (`riverForecast.ts:134`), and `OverviewPage.tsx:601` opens no river window when the clicked feature lacks segment / basin-version / river-network-version id. `loadHydroMetRiverForecast` itself has no empty-id guard. The `OverviewPage.tsx:601` guard has **no vitest pin found** (new gap). |
| `renders GFS and IFS curves with the comparison colors` (:391) | `ForecastChart` line colours / styles per series | re-homed to `src/components/charts/__tests__/ForecastChartComparison.test.tsx` | Assertions byte-identical (diffed against `git show HEAD:` :391-525). |
| `shows the IFS 6d annotation when available lead hours are 144` (:424) | `IFS 6d` markLine at cycle + 144 h, data trimmed to it | re-homed (same file) | unchanged |
| `renders an over-budget state instead of passing oversized forecast arrays to ECharts` (:451) | `pointBudgetStatus.overBudget` renders the status message and no ECharts | re-homed (same file) | unchanged; see "Symbols orphaned": no production code sets `pointBudgetStatus` any more |
| `renders a degraded state for too many retained one-point series before building ECharts options` (:480) | same, series budget | re-homed (same file) | unchanged; same note |
| `defensively caps chart option points at the shared budget` (:508) | chart option holds at most `FORECAST_CHART_POINT_BUDGET` points | re-homed (same file) | unchanged |
| `shows unavailable text when IFS is selected but absent from the response` (:527) | `<ScenarioSelector/>` shows `(暂无数据)` for a selected source missing from the data | deleted | Dead with the selector and the requirement it tested (D6). Live per-source degraded display: `M11RiverForecastPanel.test.tsx:349` "still draws the valid source and lists the failed one when one source fails validation". |

## `apps/frontend/src/pages/m11/__tests__/useHydroMetProduct.test.tsx` (4 cases; file deleted)

| test (line) | what it asserted | disposition | live pin or reason |
| --- | --- | --- | --- |
| `resolves product when basinId + concrete GFS/IFS source are present` (:77) | hook resolves a product (via the hook store, which called `loadHydroMetBootstrap`) | deleted | Correction to the fixture ("goes through the live bootstrap"): it went through `loadHydroMetBootstrap`, which after this change has no production caller (D8). That function's own behaviour stays pinned by the `loadHydroMetBootstrap` suite (`bootstrap.test.ts:260`ff). The live popups resolve the product through `fetchHydroMetLatestProduct`, pinned at `bootstrap.test.ts:540` and by the popup tests (`M11RiverForecastPanel.test.tsx:204`). |
| `does NOT fetch when source is unresolved (best); product=null + honest reason` (:89) | `resolvedSource: 'Unknown'` → no GET, reason `等待 Best Available 解析` | deleted | Dead: both live popups fetch a fixed `['GFS', 'IFS']` pair (`M11RiverForecastPanel.tsx:34`, `M11StationForcingPopup.tsx:63`); no unresolved source reaches them. |
| `does NOT fetch when basinId is absent; product=null + 请选择流域` (:99) | `basinId: null` → no GET, reason `请选择流域` | deleted | Live pins: `M11RiverForecastPanel.test.tsx:471` "shows honest empty state and resolves no product when basinId=null" and `M11StationForcingPopupGating.test.tsx:71`. |
| `dedupes concurrent same-key requests via in-flight cache` (:109) | two concurrent `loadProduct` calls → one latest-product GET | deleted | The in-flight map was the hook store's. Live dedupe is `fetchHydroMetLatestProduct`'s cache, which stores the promise when the call starts (`hydroMetBootstrap.ts:89-105`), so concurrent and sequential calls share one GET. `bootstrap.test.ts:540` pins the sequential case (two awaited calls → one GET); the concurrent case runs the same branch but is not pinned separately (minor gap). |

## Python source-text tests (per assertion; no test left empty, none deleted)

`tests/test_e2e.py::test_frontend_forecast_react_requests_segments_and_configures_echarts`

| assertion | disposition | reason |
| --- | --- | --- |
| `"includeAnalysis: true" in forecast_store` | removed | store default; the live request sends `include_analysis: false` (`bootstrap.test.ts:804`) |
| `"include_analysis" in forecast_store` | removed, not re-pointed | the literal is in `riverForecast.ts`, but there it is sent as `false`, the opposite of what this assertion stood for; the exact live value is pinned at `bootstrap.test.ts:804` |
| `"payload.segments" in forecast_store` | removed | the literal is not in the live file (`riverForecast.ts` reads `response.segments`, :152/:287); the spliced `segments` shape on the live path has no vitest fixture (gap below) |
| `"analysis_true_field" in forecast_store` | removed | the live river path never requests analysis |
| `"#2266cc"` / `"#ef7d22" in forecast_store` | re-pointed to `apps/frontend/src/lib/forecastData.ts` | type-field assertions (D3): the colours are members of the `ForecastSeries.color` union that moved unchanged. They now pin only the union; the store's colour assignment is dead, and the live window uses `#22d3ee` / `#34d399` (`M11RiverForecastPanel.tsx:36`) |
| `"markLine"`, `"起报时间" in forecast_chart` | kept | `ForecastChart.tsx` unchanged |
| `"数据源" in forecast_panel` | removed | the panel is deleted (dead) |

`tests/test_e2e_ifs.py::test_frontend_ifs_chart_contract_handles_multi_source_metadata`

| assertion | disposition | reason |
| --- | --- | --- |
| `"selectedScenarios.join(',')" in forecast_store` | removed | store-only multi-scenario request |
| `"available_lead_hours"`, `"source_id" in forecast_store` | re-pointed to `apps/frontend/src/lib/hydroMet/riverForecast.ts` | live at `riverForecast.ts:312` / `:309` (D3) |
| `"#2ca02c" in forecast_store` | re-pointed to `apps/frontend/src/lib/forecastData.ts` | type-field (colour union), as above |
| `"data?.series ?? []"`, `"#2ca02c"`, `"IFS_SIX_DAY_LEAD_HOURS = 144"`, `"IFS 6d"`, `"markLine" in forecast_chart` | kept | unchanged |

`tests/test_forecast_store_routing.py::test_run_identity_is_resolved_once_in_forecast_series_and_never_for_a_bound_run`:
docstring only. It said the unbound (no `run_id`) shape is "the live frontend one"; the only production frontend caller
of forecast-series is `lib/hydroMet/riverForecast.ts`, which sends `run_id` (pinned `bootstrap.test.ts:804`). Rewritten
to say so. No assertion changed.

## Coverage gaps on the live path (reported, not fixed)

- 480-point per-series cap at `riverForecast.ts:331-334` (`HYDRO_MET_RIVER_FORECAST_LIMIT`): no vitest pin (D9;
  confirmed: no test references the constant or a >480-point river payload).
- 8-series inspection limit at `riverForecast.ts:193-195` (`HYDRO_MET_RIVER_FORECAST_SERIES_INSPECTION_LIMIT`): no vitest
  pin (new).
- Spliced response shape (`segments` / `data` / `valid_time`, `riverForecast.ts:152`, `:287`, `:313`, `:375`): every
  vitest fixture uses `series` / `points`; no pin (new; the removed Python `payload.segments` text check was the only,
  store-side, reference).
- `OverviewPage.tsx:601`: a river click whose feature lacks segment / basin-version / river-network-version id opens no
  window. No vitest pin found (new).
- `fetchHydroMetLatestProduct` concurrent same-key dedupe: same code branch as the pinned sequential case
  (`bootstrap.test.ts:540`) but not pinned on its own (minor, new).
- Correction to D9: the query `loadHydroMetRiverForecast` sends **is** pinned in vitest. `bootstrap.test.ts:804` asserts
  the whole call with `toHaveBeenCalledWith` (path ids, `river_network_version_id`, `run_id`, `model_id`, `issue_time`,
  `variables`, `scenarios`, `include_analysis: false`) and that `forcing_version_id` is not sent; `:847` pins the IFS
  scenario. Not a gap.
- Sibling-segment identity: pinned (`riverForecast.test.ts:135`; per source `M11RiverForecastPanel.test.tsx:349`), as D9
  says.
- Spec gaps recorded in the REMOVED reasons (D6), unchanged here: per-source degraded display of the river window
  (test-pinned at `M11RiverForecastPanel.test.tsx:349`, no spec statement) and "analysis appears once with several
  scenarios" (pinned by `tests/test_e2e_ifs.py`).

## Symbols orphaned by this change

Deleted:

- `takeSeries` on the point-budget guard (`lib/forecastRenderingBudget.ts`). After the deletion it had no caller at all
  (`grep takeSeries` over `src` and `e2e`: only the deleted store) and no direct test. Removing it left
  `retainedSeriesCount` never reassigned, so it became `const` (lint `prefer-const`); no other edit.

Left in place:

- `loadHydroMetBootstrap` (`stores/hydroMetBootstrap.ts:296`): no production caller, own suite in `bootstrap.test.ts`,
  cited by the API-consumer inventories (D8). With it, `fetchHydroMetStations` (`:176`) and the private
  `getLatestProduct` (`:43`, the full-bootstrap latest-product GET), `validateLatestProduct`, `baseResult` and
  `settledError` are reachable only through it. Follow-up: a decision on the `latest-product` consumer inventory.
- `isForecastPointOverBudget` (`lib/forecastRenderingBudget.ts`): callerless before this change (tasks 1.5); not deleted.
- Residue of the point-budget guard, left as is: the live caller (`riverForecast.ts:331-334`) uses `take` and calls
  `setSourceSeriesCount` / `setSourcePointCount`, but nothing calls `status()` any more; `retainedSeriesCount` is a
  constant 0 and `seriesBudget` / `FORECAST_CHART_SERIES_BUDGET` feed only `status()`.
- `ForecastData.pointBudgetStatus`: no production code sets it after the store's removal (`M11RiverForecastPanel.tsx`
  builds `ForecastData` without it), so `ForecastChart`'s over-budget branch (`ForecastChart.tsx:182-192`) and
  `forecastPointBudgetMessage` are exercised only by the re-homed tests (:451, :480). The store was already unreachable,
  so this was true before the change; the deletion makes it visible.
- Moved unchanged (D2) but used only by tests now: the `ForecastSeries.color` members `#2266cc`, `#ef7d22`, `#2ca02c`
  (live colours are `#22d3ee` / `#34d399`), and the fields `ForecastData.source` (the one `M11Source` use outside
  `lib/m11` and `pages/m11`) and `ForecastSeries.role`, which no production code sets or reads (`M11RiverForecastPanel.tsx`
  and `ForecastChart.tsx` checked).
