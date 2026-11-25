# Frontend symbol-level dead code and coverage gaps left after the legacy page deletion (#3241)

Fixture level: standard. Risk packs: none (frontend only, no API, no state
machine). Review seats: one (correctness and test evidence).

## Why

The legacy page deletion (#3222) removed three frontend files after
checking reachability from `src/main.tsx` one file at a time. That check cannot see a dead export
inside a file that is still reachable. The deletion removed the last
production caller of a set of symbols that now survive only because tests
import them, and it removed the tests of some live behaviour. Other live
behaviour never had a test.

The issue lists every symbol with evidence; its line numbers still match
master `2f89d325f` (checked with the issue's own whole-word grep loop on
2026-11-25).

## What changes

### A. Dead code is deleted (no production behaviour changes)

- **A1** `stores/hydroMetBootstrap.ts`: the full-bootstrap path
  (`loadHydroMetBootstrap`, the private `getLatestProduct`,
  `fetchHydroMetStations`) and everything only that path uses
  (`HydroMetBootstrapStatus`, `HydroMetBootstrapResult`,
  `productAvailabilityReasons`, `validateLatestProduct`, `baseResult`,
  `settledError`, the `strictIdentity` / `stationLimit` request fields,
  imports that become unused). Its `describe('loadHydroMetBootstrap')` test
  block goes with it, after two sets of assertions inside it that cover
  live functions are moved into direct tests: the `sanitizeHydroMetMessage`
  redaction, and the only unit coverage of `fetchHydroMetStationsByIdentity`
  (query shape, runtime coordinate normalisation, the failure path); every
  other unit test mocks that function.
- **A2** the point-budget status branch: `ForecastData.pointBudgetStatus`,
  the chart branch that reads it, `forecastPointBudgetMessage`,
  `isForecastPointOverBudget`, the guard's `status()` and its write-only
  bookkeeping, `ForecastPointBudgetStatus`, `FORECAST_CHART_SERIES_BUDGET`,
  the two setter calls in `lib/hydroMet/riverForecast.ts`, and the two
  chart test cases that exercise the dead branch.
- **A3** chart data fields nothing writes or reads in production:
  `ForecastData.source`, `ForecastSeries.role`, and the three legacy
  members of the `ForecastSeries.color` union. Test fixtures that use the
  legacy colours switch to the live ones.
- **A4** `hydroMetStationFilterAvailability` (no caller at all),
  `riverForecastRequestKey` with its test, and `ForecastSeries.isAnalysis`
  with the issue-time divider it gates.

### B. Tests for live behaviour that has none (no production change)

- **B1** the 480-point cap on a river forecast series, and its `capped` flag.
- **B2** the 8-series inspection cap and its validation message.
- **B3** the discharge-click identity guard on the overview page.
- **B4** concurrent same-key latest-product requests share one GET; a
  rejected request leaves the cache.

### Spec and docs

- `frontend-lint-and-layering`, requirement "API failures SHALL resolve
  through one normalizer": the sentence that names `settledError` as a
  masking wrapper that stays is rewritten for the one wrapper left.
- Governance docs that cite the deleted call or its line numbers:
  `docs/governance/API_CONTRACT_FRONTEND_CONSUMER_EVIDENCE.md`,
  `API_CONTRACT_OPENAPI_TYPE_SYNC_EVIDENCE.md`,
  `API_CONTRACT_RETIREMENT_INVENTORY.md`.

## Decisions

- **D1 `settledError` is deleted**, with the spec sentence, instead of kept
  as an uncalled wrapper. Its only caller is the deleted function.
- **D2 The guard object is deleted, not slimmed.** At its one call site the
  guard is created, told two numbers nobody reads, and asked once to
  `take` from an array: that is `slice(0, HYDRO_MET_RIVER_FORECAST_LIMIT)`.
  The call site becomes that slice. The value is always an array there
  (a non-array has already returned `null` a few lines above), so the
  guard's non-array branch has nothing to carry over.
  `lib/forecastRenderingBudget.ts` keeps `FORECAST_CHART_POINT_BUDGET`,
  which the chart uses. (The issue recommended slimming the guard and
  listed this as the simpler alternative.)
- **D3 `isAnalysis` and the issue-time divider are deleted.** Production
  always writes `false` (`components/map/M11RiverForecastPanel.tsx`, the
  chart's only consumer), so the divider can never be drawn. If an analysis
  series returns to the chart, the divider returns with a writer.
- **D4 The Python source-text tests follow the code.** `tests/test_e2e.py`
  and `tests/test_e2e_ifs.py` assert that legacy colour literals occur in
  `forecastData.ts`. They are changed to assert the two live colours
  there; the assertion that `#2ca02c` occurs in `ForecastChart.tsx` (the
  live IFS six-day mark line) stays. The neighbouring assertions that
  `markLine` and `起报时间` occur in the chart source stay true for live
  reasons (the IFS six-day mark line; the title subtext of the non-compact
  variant) and are left as they are.

## Must preserve

- Everything the issue lists as live: `fetchHydroMetLatestProduct`,
  `prefetchHydroMetLatestProducts`, `fetchHydroMetStationsByIdentity`,
  `HYDRO_MET_STATION_LIMIT`, `HydroMetBootstrapRequest` (minus the two dead
  fields), `ForecastSeries.source`, `FORECAST_CHART_POINT_BUDGET`, the
  `'#2ca02c'` mark-line literal in `ForecastChart.tsx`, the test-only cache
  reset exports.
- The rendered output of the river forecast window and chart for every
  input production can produce: same points after the cap, same `capped`
  flag, same series colours, same mark lines.
- The coverage of `sanitizeHydroMetMessage` and of
  `fetchHydroMetStationsByIdentity`: every assertion of the removed block
  about either exists afterwards in a direct test.
- No backend file, no generated API type, no e2e spec changes.

## Not in this change

- The backend's full (non-`identity_only`) latest-product mode loses its
  last frontend caller's definition here; whether the backend mode is
  retired is a separate API-contract decision.
- Two current specs describe what D3 and the colour deletion remove:
  `analysis-forecast-curve-splicing` ("Frontend Curve Splicing Display":
  an issue-time divider between an analysis and a forecast segment) and
  `multi-source-comparison-ui` ("GFS+IFS 双曲线显示": the three legacy
  colours). Production draws neither today (no analysis series is requested;
  the live colours differ), so no scenario becomes false through this
  change that was true before it. Rewriting them belongs to #3243, which
  already lists both and names this coupling; that issue gets a note with
  the decision and the shifted line references.
- A symbol-level unused-export check (knip or similar): the issue marks it
  optional; not adopted here.
- Renaming `m11*` identifiers; spec triage of pre-single-map frontend
  specs (#3243).
