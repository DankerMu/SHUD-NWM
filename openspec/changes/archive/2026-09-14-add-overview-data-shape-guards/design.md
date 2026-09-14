## Context

- `stores/overviewData.ts` owns the overview data chain (baseline master `a21f25ee1`):
  - `getApi<T>` throws `Error(message)` on an API error and otherwise returns `unwrapApiData<T>` (bare `as T`).
  - `cached(key, loader)` stores only resolved values and deletes the entry on rejection.
- `loadOverview` has three phases:
  1. `bootstrapPromise`: `fetchBasins` + `fetchLayers(null)` via `allSettled`. A rejection sets `bootstrapError = '<which>: 暂不可用'` and `mapBootstrapLoading: false`. After both fulfil, `buildLayerStates` / `normalizeOverviewBasins` run *before* `set({ mapBootstrapLoading: false })`.
  2. `enrichmentPromise`: runs, models, queue, pipeline, versions, scoped layers via `settledValue(..., partialErrors, label)` → `'<label>: 暂不可用'` into `error`.
  3. `layerTimeEnrichmentPromise`:
     - cycles → `writeCycles(source, {status:'available'|'error'})`;
     - valid-times → `writeValidTimes(key, …)`;
     - precip index → `.catch(() => ({status:'error'}))` → `writePrecipIndex`.
- `load` awaits the three phases with `allSettled` and discards the bootstrap and layer-time settlements.
- `OverviewPage` shows one floating notice from a mutually exclusive chain: `m11-met-station-status` → `m11-overview-loading` → `m11-overview-empty` (`bootstrapError ?? error ?? …`) → `m11-precip-notice`.
- House precedent: `validateRuntimeConfig` / `validateStrictStages` in `stores/monitoring.ts` are synchronous validators that throw a Chinese `Error` inside the fetcher.

## Decisions

### D1: one guard module, validators inside the cached loaders

- New module `apps/frontend/src/lib/m11/overviewShapeGuards.ts`, next to `overviewDataContracts.ts`. It exports:
  - `class DataShapeError extends Error { readonly label: string }`, where `label` is the Chinese label from the table below and the message is `'<label>: 数据异常'`;
  - `isDataShapeError(error): error is DataShapeError`;
  - one validator per endpoint, each `(value: unknown) => T` that returns the value typed or throws `DataShapeError`.
- The minimum shape is what consumers dereference. Extra fields are ignored and nullable fields stay nullable.

| Endpoint | Label | Validator requires |
|---|---|---|
| `/api/v1/layers` | 图层目录 | array; every element a non-null object with string `layer_id`; `metadata` absent, null or object |
| `/api/v1/layers/discharge/cycles` | 起报时次 | object; `cycles` array; every entry a non-null object with string `cycle_time`; `default_cycle` absent, null or string |
| `/api/v1/layers/{layer_id}/valid-times` | 有效时次 | either a string array, or an object whose `valid_times` is a string array |
| `/api/v1/precip/{source}/{cycle}/index` | 降水索引 | object; `bounds` array of 4 finite numbers; `valid_times` string array (the same facts `m11PrecipOverlay.ts` already checks) |
| `/api/v1/basins` | 流域清单 | array; every element a non-null object with string `basin_id` |
| `/api/v1/runs` | 运行记录 | object; `items` array; every element a non-null object with string `run_id` |

- Each validator runs **inside** the `cached()` loader, after unwrap:
  - `fetchLayers`: after the `.catch(apiFetch fallback)` chain, so both branches are validated. A shape error from the primary branch must not trigger the fallback, so validate the result of the whole chain, once.
  - `fetchDischargeCycles`, `fetchLayerValidTimesForCycle`: before `normalizeLayerValidTimesResponse`.
  - `fetchPrecipIndex`: only on the success branch. The code-aware `not_mirrored` / `error` returns are unchanged.
  - `fetchBasins`: validates its own page.
  - `fetchRunsPageByStatus`: validates each page, before `mergeRunPages`.
- A thrown `DataShapeError` is a loader rejection:
  - `cached()` does not store it, so the next load retries;
  - the existing scoped paths apply unchanged (`writeCycles(…'error')`, `writeValidTimes(…'error')`, precip `'error'`, bootstrap rejected branch, enrichment `settledValue`).
- `unwrapApiData`, `getApi` and all other fetchers are untouched (ruling A deferred).

### D2: explicit 「数据异常」 state

- Store field `dataAnomalies: string[]` holds endpoint labels, deduplicated, in first-seen order. It is initialised `[]`.
  - Reset: cleared at the same place `bootstrapError` / `error` are reset when a new load starts.
  - Appended by `recordDataAnomaly(label)`, and only when `isCurrentRequest()`.
- Recording points, each keyed on `isDataShapeError(reason)`:

| Recording point | Behaviour |
|---|---|
| bootstrap rejected branch | the existing `which` labels (`basins`, `layers`) and `safeM11ErrorMessage` stay. Fallback word per side: `数据异常` for a shape error, `暂不可用` otherwise. Both sides same word → `'basins + layers: <word>'` (unchanged format). Mixed → `'basins: <word>；layers: <word>'` (basins first). One side → `'<which>: <word>'`. Record each shape error's `label` |
| `settledValue(result, errors, label, onShapeError?)` | new optional 4th parameter `onShapeError?: (error: DataShapeError) => void`. On a rejected shape error, push `'<label>: 数据异常'` and call it; other rejections unchanged. Call sites inside `loadOverview` pass `recordDataAnomaly`-backed callbacks |
| cycles, valid-times, precip rejection handlers (`overviewData.ts` `() => writeCycles(…)`, `() => writeValidTimes(…)`, `.catch(() => …)`) | change `() =>` to `(reason) =>`, record when `isDataShapeError(reason)`; the written state stays `{status:'error'}` |

- Enrichment re-reads `/basins` and `/layers` from the same cached promise, so one malformed basins payload yields both `bootstrapError: 'basins: 数据异常'` and `error` containing `'basins: 数据异常'`. `dataAnomalies` records the label once (dedup). This is expected; do not "fix" it.
- `clearOverviewDataCache` does not reset `dataAnomalies`; every page load starts a new `loadOverview`, which does.
- `OverviewPage` renders `<M11FloatingNotice testId="m11-data-anomaly">` with 「数据异常：{labels joined by 、}返回格式不符，已按不可用处理」.
  - Chain position: after `m11-overview-empty`, before `m11-precip-notice`.
  - A hard bootstrap failure already names 数据异常 in its own text and outranks it. The anomaly notice outranks decorative precip notices, so a malformed precip index shows 数据异常 rather than the generic index-error text.
- Why not a new `'invalid'` variant in the scoped unions: the three-state layer text machinery (`pending` / `error` / `fail-closed`, `buildLayerStates`) and the control bar would need a fourth arm at every switch. The notice makes the fault explicit, and the scoped state stays fail-closed.

### D3: no generic bootstrap normalization catch

- Not added. D1 rejects every known malformed trigger before normalization. A non-shape throw there is a code bug, and labelling it 数据异常 would blur the distinction the ruling asks for. Existing behaviour: `load`'s catch settles `加载总览数据失败`.

### D4: control bar and the #2347 artifacts

- Stale comments that describe malformed payloads passing through as `available` are updated to point at D1: `overviewData.ts` near `dischargeCyclesDefaultCycle` and in `pairRecord`, plus the name of the test in `overviewDataSourceSelection.test.ts` that feeds `cycles: null` (its assertions stay).
- `deriveM11ControlBarModel` is unchanged. Update its comment to say store-side guards (D1) reject malformed cycles before render, and that the #2347 region boundary is the backstop. No `entry?.` churn.
- `OverviewPageRegionBoundaries.test.tsx` (#2347 E4): its API mock `cycles: [null, …]` now produces 数据异常, not a render throw.
  - Re-seam: `vi.mock('@/pages/m11/M11BottomControlBar', …)` keeping the real module but making `deriveM11ControlBarModel` (or `M11BottomControlBar`) throw.
  - Keep every existing assertion about the fallback and the surviving regions. Serve valid cycles from the mock.
- MODIFIED spec delta for `frontend-render-failure-containment`: the scenario trigger becomes "the control bar model derivation throws during render". The requirement text is unchanged.

### D5: review convention

- Add one line to `openspec/project-profile.md` under Risk axes: "Frontend API consumers: a new `unwrapApiData` / `getApi` consumer ships with a malformed-payload test (wrong container, null element, wrong element type)."
- No new document. The shared skills are not edited.

### D6: tests

- **Store tests** use the shared `src/test/overviewDataFixture.ts` `mockApi` in a new `src/stores/__tests__/overviewDataShapeGuards.test.ts`.
- **Validator unit tests** go in `src/lib/__tests__/overviewShapeGuards.test.ts` (same directory convention as `m11OverviewDataContracts.test.ts`): every row accepts a valid payload and rejects wrong container, null element and wrong element type.
- **Page tests** go in a new `src/pages/__tests__/OverviewPageDataAnomaly.test.tsx`, following the `OverviewPageRegionBoundaries.test.tsx` harness on the default `/` with gfs.

## Risks

- A guard stricter than the backend contract would turn valid data into 数据异常. Validators require only fields that consumers already dereference and that the OpenAPI marks as required. Validator tests include the real fixture payloads from existing tests as accept cases.
- The notice chain is mutually exclusive, so the anomaly notice can hide a precip notice. That is intended (D2).
