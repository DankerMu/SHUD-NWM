## Risk Triage

```text
Issue type: feature (defensive data-boundary guards), #2129 ruling B
Project profile: NHMS (openspec/project-profile.md)
Blast radius: medium-high
Fixture level: expanded
Repair intensity: medium
Upstream suggested level: absent (ruling comment on #2129)
Why:
- touches the overview store's three-phase load machinery (bootstrap settle, enrichment error text, layer-time scoped writes) and adds a store field
- changes the mutually exclusive notice chain on `/`
- validators that are stricter than the real backend contract would turn valid production data into 数据异常 (user-visible regression)
- re-seams a #2347 test and modifies an archived spec scenario
Selected risk packs:
- Error handling / rollback / partial outputs
- Schema / columns / units / field names (validator minimum shapes vs OpenAPI)
- Concurrency / shared state / ordering (per-load anomaly state, isCurrentRequest, cache non-storage)
- Legacy compatibility / examples (existing store/page tests and fixtures must stay green unmodified)
OpenSpec change: add-overview-data-shape-guards (generated)
Evidence floor:
- openspec validate add-overview-data-shape-guards --strict --no-interactive
- cd apps/frontend && pnpm exec tsc --noEmit -p tsconfig.app.json && pnpm check:types && pnpm test && pnpm build
- node-27 frontend-only deploy + browser receipt (client-side injected malformed cycles, both kinds)
```

## Risk Pack Selection

| Pack | Selected | Reason |
|---|---|---|
| Public API / CLI / script entry | not selected | No API, route or URL-state change. |
| Config / project setup | not selected | No dependency; one profile line. |
| File IO / path safety / overwrite | not selected | Browser only. |
| Schema / columns / units / field names | selected | Validators encode minimum payload shapes; must match OpenAPI-required fields and real fixtures. |
| Auth / permissions / secrets | not selected | None. |
| Concurrency / shared state / ordering | selected | `dataAnomalies` is per-load store state written from async handlers; stale generations must not write; cache must not store rejections. |
| Resource limits / large input / discovery | not selected | Validators are O(n) over arrays already iterated by consumers. |
| Legacy compatibility / examples | selected | Existing overview store/page tests, `overviewDataFixture.ts` payloads, notice-chain testids. |
| Error handling / rollback / partial outputs | selected | Core: degrade-to-state semantics, bootstrap settle. |
| Release / packaging / dependency compatibility | not selected | None. |
| Documentation / migration notes | not selected | One profile line only. |
| Published NHMS artifacts / display identity (domain) | not selected | Tile URLs and `(source, cycle)` identity unchanged for valid payloads. |
| Hydro-met time series / forcing windows (domain) | not selected | No time logic change. |

## Must preserve

- Valid payloads: identical store state, layer text, request set and DOM as master. Every existing test file keeps every assertion. If an existing fixture payload fails a validator, stop and report (the fixture or the validator is wrong); do not loosen or edit silently.
- `cached()` semantics: resolved values cached as before; shape errors not cached.
- Scoped state unions (`DischargeCyclesState`, `ValidTimesState`, `PrecipIndexState`) and the `pending` / `error` / `fail-closed` / `cycle-not-listed` layer text machinery unchanged.
- Non-shape errors keep their current text (`'<label>: 暂不可用'`, `加载总览数据失败`) and current notice.
- `fetchLayers` apiFetch fallback still runs on an API/network error of the primary branch.
- Precip code-aware returns (`not_mirrored`, `PRECIP_WINDOW_INCOMPLETE` → `error`) unchanged; overlay's own guards stay.
- `m11-overview-empty` remains the only render surface of `bootstrapError` / `error`, and outranks everything but met-station status and loading.
- #2347 boundaries and their fallback assertions.
- `overviewDataSourceSelection.test.ts` "source cycle list is malformed" (`cycles: null`): after D1 the record becomes scoped `error` and one anomaly is recorded; its assertions (`'Layer has no valid times.'`) must stay green unmodified.

## Seams under test

- Validator unit tests: pure functions, no mocks.
- Store: `useOverviewDataStore.getState().loadOverview(query)` with `src/test/overviewDataFixture.ts` `mockApi(overrides)` and `resetOverviewDataTestState()`, as in `src/stores/__tests__/overviewData.test.ts`. Assert `cyclesBySource`, `validTimesByCycle`, `precipIndexByCycle`, `bootstrapError`, `error`, `mapBootstrapLoading`, `dataAnomalies`; count loader calls across two loads to prove non-caching.
- Page: `OverviewPage` harness as in `src/pages/__tests__/OverviewPageRegionBoundaries.test.tsx` (mocked `client.GET`, maplibre stub) on the default `/` (gfs).
- Non-default pair for valid-times: `/?source=ifs` with an ifs `/cycles` payload carrying a valid `default_cycle`, so `fetchLayerValidTimesForCycle` is reached (store test).

## Tasks

### 1. Fixture

- [x] 1.1 Proposal, design, tasks, ADDED + MODIFIED deltas.
- [x] 1.2 Fixture review; `openspec validate --strict`.
  - Iteration 1: revise, 3 gaps, all applied:
    - E3/E8 red states and the proposal failure description corrected (shared cached promise; basins → `加载总览数据失败`, layers → indefinite loading);
    - D3 generic bootstrap catch dropped (unreachable behind D1, would mislabel code bugs);
    - D2 wiring pinned: `DataShapeError.label`, `settledValue` 4th parameter, mixed bootstrap text, handler reasons.
  - Iteration 2: pass. Notes applied: E3 mixed red state recorded; E5 adds a page assertion that a runs shape error surfaces as `m11-data-anomaly` while basins are non-empty.
  - Iteration 1 notes applied: module path unified; `cycles: null` legacy test named; E6 mock swap; spec wording yields to earlier notices.

### 2. Guards

- [x] 2.1 `lib/m11/overviewShapeGuards.ts` (D1) + unit tests E1.
- [x] 2.2 Wire validators into the six fetchers (D1).

### 3. State and surface

- [x] 3.1 `dataAnomalies`, recording points, 「数据异常」 error text (D2).
- [x] 3.2 `m11-data-anomaly` notice in the chain (D2).
- [x] 3.3 Store tests E2–E6; page tests E7–E8; re-seam #2347 E4 (D4) as E9.
- [x] 3.4 Stale comments (D4); profile convention line (D5).

### 4. Close-out

- [x] 4.1 Toolchain E10; node-27 frontend-only deploy and receipt E11 (`docs/runbooks/receipts/2026-09-14-issue-2129-overview-data-anomaly-node27/`).
- [ ] 4.2 PR refs #2129 (does not close it unless every ruling-B item is satisfied); archive after merge.

## Evidence Mapping

| ID | Seam | Input | Expected |
|---|---|---|---|
| E1 | validators | per endpoint: valid payload (including payloads copied from existing test fixtures), wrong container (including cycles `cycles: null`), `null` element, wrong element type (string array for cycles/basins/layers/runs items), missing required field; valid-times both accepted forms | accept returns the value; each reject throws `DataShapeError` with the table label; `isDataShapeError` true only for it |
| E2 | store, default `/` | gfs `/cycles` → `cycles: [null, <valid>]`; separately `cycles: ['2026-05-18T00:00:00Z']` | `cyclesBySource.gfs` is `{status:'error'}`; `dataAnomalies` equals `['起报时次']`; no throw; a second `loadOverview` without clearing the cache re-requests `/cycles` (the rejection was not cached); red pre-change: store holds `available` with the malformed payload |
| E3 | store | `/basins` → `{ items: [...] }` (non-array); separately `/layers` → `[null]`; separately both, `/basins` malformed and `/layers` API error | `mapBootstrapLoading` false; basins: `bootstrapError === 'basins: 数据异常'`, `error` contains `basins: 数据异常`, `dataAnomalies` equals `['流域清单']` (once); layers: `bootstrapError === 'layers: 数据异常'`, `['图层目录']`; mixed: `bootstrapError === 'basins: 数据异常；layers: 暂不可用'` (API error via the `fetchLayers` apiFetch fallback rejecting, as in `overviewDataSourceSelection.test.ts`). Red pre-change: mixed → `bootstrapError === 'layers: 暂不可用'`; basins → `error: '加载总览数据失败'` with no 数据异常; layers → `bootstrapError` null and `overview.bootstrap` null |
| E4 | store, `/?source=ifs` | ifs `/valid-times` → `{ layer_id: 'discharge' }` (no `valid_times`) | `validTimesByCycle[ifs|cycle]` is `{status:'error'}`, discharge layer shows the existing error text (not pending), `dataAnomalies` has `有效时次`; red pre-change: layer text stays pending |
| E5 | store | precip index success with `bounds: [1,2,3]`; separately `/runs` page `items: 'x'` | precip state `{status:'error'}` + `降水索引`; runs → `error` contains `runs: 数据异常` (label per existing `settledValue` call) + `运行记录`; map bootstrap unaffected; page-level: with non-empty basins the runs anomaly surfaces as `m11-data-anomaly` (naming 运行记录), not `m11-overview-empty` |
| E6 | store | API error (non-shape) on `/cycles` and `/basins`; a stale generation delivering a shape error after a newer load started; a load with a shape error followed by a load whose mocks were swapped to valid payloads | error text unchanged (`暂不可用`), `dataAnomalies` empty for non-shape errors; the stale generation writes nothing; the second (valid) load ends with `dataAnomalies` empty |
| E7 | page, default `/` | gfs `/cycles` → `[null, <valid>]`; separately string array | `m11-data-anomaly` with 「数据异常：起报时次返回格式不符，已按不可用处理」; zero `region-error-*` / `route-error-fallback`; `m11-fullscreen-map`, legend, `m11-bottom-control-bar` present; red pre-change: `[null]` → `region-error-control-bar`, string array → no anomaly notice |
| E8 | page | `/basins` non-array; separately `/layers` → `[null]` | `m11-overview-empty` text contains `数据异常`; `m11-overview-loading` absent after settle. Red pre-change: basins → `m11-overview-empty` shows 加载总览数据失败 without 数据异常; layers → `m11-overview-loading` persists (bounded wait) |
| E9 | #2347 E4 re-seam | `deriveM11ControlBarModel` mocked to throw, valid payloads | same assertions as before (control-bar fallback, other regions present) |
| E10 | toolchain | evidence floor | tsc app, check:types, full vitest (count before → after), build, openspec strict all green |
| E11 | node-27 browser receipt | frontend-only deploy of the PR head build (backup kept); Playwright `page.route` rewrites gfs `/cycles` to `[null, …original]`, then separately to the string array of original `cycle_time`s | `m11-data-anomaly` visible with the label, zero boundary fallbacks, control bar present, tiles load; unmodified page: no anomaly notice and zero fallbacks; screenshots + `capture.log` |
