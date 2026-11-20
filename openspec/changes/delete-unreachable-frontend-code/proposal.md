# Delete the frontend code that is unreachable from the app entry point (#3222)

## Why

Reachability from `apps/frontend/src/main.tsx` over non-test imports (script: `artifacts/issue-3222/reach.py`, run on
master f45e8f3a8: 114 production source files, 96 reachable) leaves 18 files unreachable. Ten of them are the two
evidence-lane libraries (`src/lib/c4DisplayEvidence`, `src/lib/riverClickEvidence`), which the Playwright lanes import.
The other eight are dead:

| file | lines | imported by |
| --- | --- | --- |
| `src/components/ui/dropdown-menu.tsx` | 143 | nothing |
| `src/components/forecast/ForecastPanel.tsx` | 106 | nothing |
| `src/components/ui/tabs.tsx` | 45 | nothing |
| `src/components/forecast/SegmentInfo.tsx` | 44 | `ForecastPanel.tsx` only |
| `src/hooks/usePagination.ts` | 27 | nothing |
| `src/components/ScenarioSelector.tsx` | 57 | `ForecastPanel.tsx` and one test |
| `src/pages/m11/useHydroMetProduct.ts` | 76 | its own test only |
| `src/stores/hydroMetProductData.ts` | 94 | `useHydroMetProduct.ts` and its test only (not named in the issue; same rule) |

`src/stores/forecast.ts` (494 lines) is reachable only through `import type { ForecastData }` in
`components/charts/ForecastChart.tsx` and `components/map/M11RiverForecastPanel.tsx`. Its zustand store
(`useForecastStore`: `selectSegment`, `toggleScenario`, `setRequestContext`, `fetchForecast`, …), `fetchForecastSeries`
and every normaliser in the file have exactly one non-test user, `ScenarioSelector.tsx`, which is dead. The live river
popup fetches through `src/lib/hydroMet/riverForecast.ts`.

Tests keep this code looking alive: `src/__tests__/ForecastComparison.test.tsx` (ten store / selector cases, five
`ForecastChart` render cases and one `ScenarioSelector` render case in one file), `src/pages/m11/__tests__/useHydroMetProduct.test.tsx`, and two Python
source-text tests (`tests/test_e2e.py::test_frontend_forecast_react_requests_segments_and_configures_echarts`,
`tests/test_e2e_ifs.py::test_frontend_ifs_chart_contract_handles_multi_source_metadata`) that read the dead store and
`ForecastPanel.tsx` as text.

## What changes

1. Delete the eight files above.
2. Delete `src/stores/forecast.ts`. The type declarations live code uses (`ForecastData` and the declarations in that
   file it references: `ForecastSeries`, `ForecastSeriesPoint`) move unchanged to `src/lib/forecastData.ts`, which keeps
   importing `M11Source` (`lib/m11/queryState.ts`) and `ForecastPointBudgetStatus` (`lib/forecastRenderingBudget.ts`),
   both leaf modules; the importers switch path.
   `ForecastSegmentInfo`, `FetchForecastOptions` and `ForecastRequestContext` have no live user and go with the store.
3. Tests follow the code they test, per test, not per file (see Decisions D3).
4. `@radix-ui/react-dropdown-menu` and `@radix-ui/react-tabs` lose their only importers and are removed from
   `package.json` and `pnpm-lock.yaml`.
5. Governance inventories and ten spec requirements in four capabilities stop naming or requiring the deleted
   code (D6).

No runtime behaviour changes: nothing reachable from `main.tsx` changes except two `import type` paths. **Ten spec
requirements are touched (five removed, five reworded) because the deleted code was their only implementation or
they named it** (D6): they described a forecast
page that the single-map page replaced and that has not been routed since.

## Decisions

- D1. The rule is reachability from `src/main.tsx` over non-test imports, applied to whole files, plus the one
  symbol-level case the issue names (the forecast store). No general unused-export sweep: that needs a tool the repo
  does not have (knip) and is a separate decision.
- D2. `src/lib/forecastData.ts` rather than a types-only `stores/forecast.ts`: a file under `stores/` with no store
  misleads the next reachability read, and `lib/` is the lowest layer both importers may use.
- D3. Test disposition is decided **per test case** from what its assertions exercise, and recorded in
  `deleted-tests.md` in this change (file, test name, what it asserted, why it is safe to delete or where it went):
  - `ForecastComparison.test.tsx` (line numbers on master f45e8f3a8): the ten cases at `:88 .. :375` drive
    `useForecastStore` / `ScenarioSelector` and are deleted; the five `ForecastChart` render cases at `:391`, `:424`,
    `:451`, `:480`, `:508` are kept and re-homed as `src/components/charts/__tests__/ForecastChartComparison.test.tsx`
    with their fixtures, assertions unchanged; the case at `:527` ("shows unavailable text when IFS is selected but
    absent") renders `<ScenarioSelector/>` and is deleted with the requirement it tested (D6).
  - A store test is deleted only after checking that the behaviour it pins is either dead with the store or already
    pinned on the live path (`src/lib/hydroMet/riverForecast.ts` and its tests: point budget, series budget, sibling
    segment identity, scoped request). Where the live path has the behaviour but no test pins it, the gap is
    **reported** in the ledger, not silently dropped and not fixed here.
  - The two Python source-text tests: assertions on `ForecastChart.tsx` stay (pointed at the same file); an assertion
    on store text whose behaviour is live elsewhere is re-pointed at the live file (`available_lead_hours` and
    `source_id` in `tests/test_e2e_ifs.py:185-186` are live at `lib/hydroMet/riverForecast.ts:309`/`:312`); assertions on
    behaviour that only the store or `ForecastPanel.tsx` had are removed; type-field assertions follow the types to
    `src/lib/forecastData.ts`. A test left with no assertion is deleted and listed.
  - `tests/test_forecast_store_routing.py:442`: the docstring's claim about the "live frontend" store is rewritten to
    name the live caller (`lib/hydroMet/riverForecast.ts`, which does send `run_id`), so it is true.
- D4. **Not moved** (the issue's optional clause, "where that needs no behaviour change"):
  - `src/lib/c4DisplayEvidence` and `src/lib/riverClickEvidence` are not test helpers: they are the libraries of the
    live Playwright evidence lanes (`playwright.c4-display-*.ts`, `e2e/live-c4-display.spec.ts`), typechecked by
    `tsconfig.node-playwright.json` and proven only by a node-27 live receipt that needs operator-approved pins. Moving
    2,900 lines for taxonomy puts that lane at risk for no runtime effect.
  - `src/test/` is the vitest setup/support directory (`vitest.config.ts` `setupFiles`), excluded from the app bundle
    by reachability and from the Playwright typecheck by config. It stays.
- D5. Governance docs are edited to stay true, not to re-argue their decisions: the `forecast-series` and
  `latest-product` routes keep live frontend consumers (`lib/hydroMet/riverForecast.ts`, `stores/overviewData.ts`,
  `stores/hydroMetBootstrap.ts`), so no API retirement status changes. A citation of a deleted test as evidence is
  **replaced** by the live test that pins the same thing (the "latest-product called once" evidence at
  `API_CONTRACT_FRONTEND_CONSUMER_EVIDENCE.md:86` and `API_CONTRACT_RETIREMENT_INVENTORY.md:135` →
  `stores/__tests__` bootstrap dedupe test, `bootstrap.test.ts:540`), not just removed; a claim that the "full
  bootstrap" call is a live consumer is corrected (see D8).
- D6. Spec disposition (found in fixture review: live specs still required the deleted code).
  - `multi-source-comparison-ui`: REMOVED "Scenario 选择器控件", "数据请求参数传递", "IFS 数据不可用时的降级显示". The four
    requirements about curve rendering, lead-hour annotation, issue-time info and API response metadata stay: `ForecastChart`
    and the API implement them.
  - `forecast-migration`: REMOVED "Forecast panel", "Forecast state management".
  - `engineering-scaffold`: MODIFIED "shadcn/ui component library" (Tabs, DropdownMenu dropped; unused generated
    components are not kept) and "Zustand state management" (no forecast store).
  - `frontend-lint-and-layering`: MODIFIED "`pages/` SHALL be a leaf layer" — the invariant stays, the transitional
    clauses (identical exports, the enumerated seven importers, path-string-only test edits) go: two of the named
    importers are deleted here, and an importer list is a fact of the tree, not a contract.
  - What replaced the removed surface is already specified: `map-feature-popups` ("点击河段要素弹出 q_down 预报曲线",
    "popup 源解析与 honest-display 不变量", "Curve windows SHALL NOT offer a forecast-source switch") and
    `mobile-curve-sheet`.
  - `multi-source-comparison-ui` MODIFIED "多源曲线渲染": the single-source scenario no longer says "selected"; it is
    about what the chart data holds. `forecast-migration` MODIFIED "River segment layer": the click opens the river
    curve window, not the removed panel.
  - Two rules lose their only spec statement and are recorded as spec gaps in the REMOVED reasons, not closed here:
    the per-source degraded display of the river curve window (pinned by `M11RiverForecastPanel.test.tsx`) and
    "analysis appears once with several scenarios" (pinned by `tests/test_e2e_ifs.py`).
  - `multi-source-comparison-ui` "起报时刻信息增强" (a header line with issue time and sources) has no implementation in
    the tree today, dead or live. That drift predates this change and is not touched; with the other specs of the
    pre-single-map frontend (`forecast-frontend`, `national-overview-page`, …) it belongs to a spec triage of its own
    (reported on the issue).
- D7. Dependencies: removing the two Radix packages changes `pnpm-lock.yaml` (frontend only; `uv.lock` is untouched).
- D8. Symbols orphaned **by this change** in live modules:
  - `takeSeries` on the point-budget guard (`lib/forecastRenderingBudget.ts:34`) has only the deleted store as caller.
    It is deleted with its tests if, after the deletion, no non-test caller remains (the implementer verifies; the
    live path uses `setSourceSeriesCount`, `riverForecast.ts:332`).
  - `loadHydroMetBootstrap` (`stores/hydroMetBootstrap.ts:296`) loses its last non-test caller
    (`hydroMetProductData.ts`). It is **not** deleted here: it has its own test suite and is cited as API-consumer
    evidence, so removing it is a decision about the `latest-product` consumer inventory. Reported as a follow-up.
- D9. Coverage gaps on the live path that the deleted store tests never covered but resemble (reported, not fixed):
  the 480-point cap at `riverForecast.ts:331-334` and the query `loadHydroMetRiverForecast` sends (`run_id`,
  `river_network_version_id`, `issue_time`, `scenarios`) have no vitest pin; the mocked e2e partly gates the query
  (`e2e/support/riverWindow.mocked.ts:388`). Sibling-segment identity is pinned (`riverForecast.test.ts:135`).

## Fixture

```text
Issue type: deletion-only cleanup + one spec requirement restated as an invariant
Fixture level: expanded (ledger required; four specs touched)
Blast radius: deleting a file the app does reach breaks the build (caught by tsc/vite); deleting a test that pinned
              live behaviour loses coverage silently (the risk D3 exists for)
Selected risk packs: Legacy compatibility (tests, inventories and specs that name or require deleted code)
Evidence floor: see tasks.md
```
