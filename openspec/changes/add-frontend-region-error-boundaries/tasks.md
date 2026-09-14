## Risk Triage

```text
Issue type: feature (defensive UI containment), #2347 (split from #2129 ruling C)
Project profile: NHMS (openspec/project-profile.md)
Blast radius: medium
Fixture level: compact
Repair intensity: low
Upstream suggested level: absent (issue body S–M)
Why:
- frontend-only, additive wrappers; no API, data, store or URL-state change
- touches the top-level route tree and the overview page composition (control-bar input seam), so existing page tests are the must-preserve oracle
- boundaries only act on render throws; happy paths render identical DOM plus wrapper-free output (boundary renders children directly)
Selected risk packs:
- Error handling / rollback / partial outputs
- Legacy compatibility / examples (existing page/control-bar tests and DOM testids)
OpenSpec change: add-frontend-region-error-boundaries (generated)
Evidence floor:
- openspec validate add-frontend-region-error-boundaries --strict --no-interactive
- cd apps/frontend && pnpm exec tsc --noEmit -p tsconfig.app.json && pnpm check:types && pnpm test && pnpm build
- node-27 frontend-only deploy + browser receipt (client-side injected malformed cycles)
```

## Risk Pack Selection

| Pack | Selected | Reason |
|---|---|---|
| Public API / CLI / script entry | not selected | No API or route path change. |
| Config / project setup | not selected | No new dependency. |
| File IO / path safety / overwrite | not selected | Browser only. |
| Schema / columns / units / field names | not selected | No data shape change. |
| Auth / permissions / secrets | not selected | `RBACGate` untouched; the boundary wraps outside it. |
| Concurrency / shared state / ordering | not selected | No store writes. Reset keys are read-only props. |
| Resource limits / large input / discovery | not selected | None. |
| Legacy compatibility / examples | selected | Existing tests assert testids and mount seams (e.g. the `m11-bottom-control-bar` synchronous seam). The control-bar prop changes shape. |
| Error handling / rollback / partial outputs | selected | Core of the change: fallback, retry, reset semantics. |
| Release / packaging / dependency compatibility | not selected | No dependency; bundle grows by one small component. |
| Documentation / migration notes | not selected | None. |
| Published NHMS artifacts / display identity (domain) | not selected | Tiles and layer identity untouched. |
| Hydro-met time series / forcing windows (domain) | not selected | No time or identity logic touched. |

## Must preserve

- Happy-path DOM: every existing testid and ARIA label renders as before. Boundaries add no wrapper element around children in the non-error state.
- `deriveM11ControlBarModel` and `M11BottomControlBar` signatures and behaviour; every existing test file keeps every assertion (`M11BottomControlBar.test.tsx`, `OverviewPage*.test.tsx`, `M11Shell.test.tsx`, `AppRoutes.test.tsx`, `AppLegacyRoutes.test.tsx`, monitoring tests).
- Control-bar re-derivation dependencies unchanged (same memo inputs).
- `Suspense` fallback 「加载中...」 is still shown while a lazy page loads.
- `RBACGate` behaviour on `/monitoring` and `/ops`.
- Legacy redirects (`LegacyRedirect`) unchanged.
- Existing `OverviewPage*`, `M11Shell` and monitoring tests render zero `region-error-*` / `route-error-fallback` testids on their happy paths. Add one assertion in an existing happy-path overview test and one in a monitoring test.

## Seams under test

- `RegionErrorBoundary` unit tests with a throwing test child (Testing Library, vitest). Silence the expected `console.error` per test and assert it was called with the region name.
- The exported `AppFrame` in `MemoryRouter` (pattern: `src/__tests__/AppLegacyRoutes.test.tsx`). `vi.mock` page modules so one throws on render and one factory throws (chunk failure). `AppShell` needs only `useMonitoringStore.setState({ runtimeConfig, runtimeConfigError: null })` (as in `OverviewPageValidTimeCorrection.test.tsx`). `SiteHeader` imports no stores.
- `OverviewPage` harnesses (`OverviewPagePrecipOverlay.test.tsx` / `OverviewPageValidTimeCorrection.test.tsx` mock API pattern) on the default `/`, with `/api/v1/layers/discharge/cycles?source=gfs` answering `cycles: [null, <valid entry>]` plus a valid `default_cycle` (reachability trace in design D3).
- `MonitoringPage` render with one child mocked to throw (`vi.mock` of that component module).

## Tasks

### 1. Fixture

- [x] 1.1 Proposal, design, tasks, ADDED delta.
- [x] 1.2 Fixture review; `openspec validate --strict`.
  - Iteration 1: revise, 8 gaps, all applied:
    - panels reset keys: popup selection; boundary narrowed to the two panels;
    - E4/E8 pinned to default `/` gfs, with the store-throw trace recorded;
    - proposal/D2 Suspense wording;
    - E3 via exported `AppFrame` + `AppLegacyRoutes` pattern + harness recipe;
    - chunk-failure retry semantics;
    - D4 TrendPanel boundary inside the grid wrapper;
    - resetKeys guard (`prevState.error`) + E2 sub-case;
    - happy-path zero-fallback Must preserve row.
  - Iteration 2: pass. Notes applied:
    - E3 navigates via a test-side `useNavigate` helper to a route that really renders, and sets an allowed role for `RBACGate`; the render-throw and import-reject mocks live on different page modules;
    - the header scenario is covered by E3 (structure) and E8 (live), not E4;
    - the panels fallback needs a floating position class;
    - `metadata` stays derived inside the input memo.

### 2. Boundary

- [x] 2.1 `components/layout/RegionErrorBoundary.tsx` (D1), `RouteErrorBoundary`, and the exported `AppFrame` used by `App` (D2).
- [x] 2.2 Tests E1–E3.

### 3. Regions

- [x] 3.1 Overview regions and control-bar input seam (D3).
- [x] 3.2 Monitoring regions (D4).
- [x] 3.3 Tests E4–E6.

### 4. Close-out

- [ ] 4.1 Toolchain E7; node-27 frontend-only deploy and receipt E8.
- [ ] 4.2 PR closes #2347; archive after merge.

## Evidence Mapping

| ID | Seam | Input | Expected |
|---|---|---|---|
| E1 | `RegionErrorBoundary` unit | child throws on render | fallback `role="alert"` with the testId and 「此区域加载失败」; the raw error message is not in the DOM; a sibling outside the boundary still renders; `console.error` called with the region name |
| E2 | same | child throws once (a flag flips after the first throw), then retry clicked; separately, `resetKeys` changed after the error; separately, `resetKeys` changed in the same render that throws | retry → child content rendered; key change after the error → child rendered without clicking; key change in the same render as the throw → fallback still shown |
| E3 | exported `AppFrame` in `MemoryRouter` | page module mocked to throw on render, then navigate to a different route; separately, a page module whose import rejects | header present plus `route-error-fallback` with 「重试」 and 「刷新页面」; after navigating to a different route, that route renders; the import-rejection case also shows `route-error-fallback` (retry is NOT asserted to recover it, see D2) |
| E4 | `OverviewPage` harness, default `/` | `/api/v1/layers/discharge/cycles?source=gfs` → `cycles: [null, <valid>]` with a valid `default_cycle` | `region-error-control-bar` shown; `m11-fullscreen-map`, legend and layer switcher testids present. Red pre-change: the test fails with an uncaught render error / `region-error-control-bar` absent. Record it |
| E5 | `OverviewPageValidTimeCorrection` mount seam | unchanged test | `m11-bottom-control-bar` still asserted synchronously and green, with no assertion edits |
| E6 | `MonitoringPage` | `JobsTable` mocked to throw | `region-error-jobs` shown; `SummaryBar`, `StageList` and `TrendPanel` still rendered |
| E7 | toolchain | evidence floor | `tsc app`, `check:types`, full vitest (count before → after recorded), build all green |
| E8 | node-27 browser receipt | frontend-only deploy of the PR head build (same method as #2336, backup kept); Playwright `page.route` rewrites the default page's `/api/v1/layers/discharge/cycles?source=gfs` response to `cycles: [null, …original]`, keeping `default_cycle` | control-bar fallback visible, map tiles still load, header present; screenshot plus `capture.log`; also the unmodified default page renders with zero fallbacks |
