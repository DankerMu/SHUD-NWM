# The station layer keeps the basins that loaded when one basin fails

Issue: #2694 (frontend decision; backend fixed by PR #2717, deployed). Fixture level: **compact**.
Risk packs: **Honest partial state (never present a partial layer as complete)**.

## Why

`fetchAllStations` (`apps/frontend/src/stores/stationLayerData.ts`) loads the visible basin contexts in one
loop; the first request that throws rejects the whole load, the store keeps `data: null` and the national
station layer is empty. In September one basin (`basins_hlj_vbasins`, an 84 s query against a 30 s role
timeout) blanked the public layer for days. Since #2699 (2026-10-07) about fifty basin versions return
stations instead of none, so one failing basin now hides far more.

Owner decision (2026-10-08 CST): degrade per basin — render the basins that loaded, say which failed.

## What changes

1. `fetchAllStations`, per basin context (the contexts actually attempted: the de-duplicated list cut to
   `STATION_CONTEXT_CAP`, minus those the client cap stops before):
   - **Latest-product lookup** (only when the context carries a resolved source): any error of
     `fetchHydroMetLatestProduct` is **not** a basin failure. The context falls back to the basin-only
     identity `{ basinVersionId: context.basinVersionId }`. A basin without a product for that source answers
     404 `QHH_LATEST_PRODUCT_UNAVAILABLE` by design, and since #2699 the basin-only list is the stations of
     the basin's latest displayable run (or an empty list), so the fallback shows what is displayable and
     nothing else. (Today that 404 rejects the whole layer.)
   - **Station pages** (first page and following pages) run inside a `try`/`catch`. When one throws: the
     `basinId` is appended to a new `failedBasinIds: string[]` of `StationLayerData` (request order, no
     duplicates); stations of that basin already appended from earlier pages stay; `truncated` becomes true
     and `totalKnown` false; the loop continues with the next context.
   - `total`: a basin adds its `total_count` when its first page succeeded, as today, and nothing when the
     first page failed. With `totalKnown === false`, `total` is a lower bound.
   - If **every** attempted context failed (counted per context, not per de-duplicated basin id), the function rethrows the first station-page error thrown:
     the store then behaves as today (`data: null`, `error` set) and stations fetched from earlier pages are
     dropped. An all-failed load is an error, never an empty "loaded" layer. A single-context request that
     fails on a later page is this case; the existing test "surfaces a mid-pagination error without silently
     flagging a complete load" (`stationLayerData.test.ts` ~:272) passes unchanged.

   Nothing else changes: cap handling, pagination, de-duplication of contexts, request keys, the in-flight
   map, `clear`.
2. `MetStationLayerModel` (`apps/frontend/src/pages/m11/useStationLayer.ts`) exposes `failedBasinIds`
   (`[]` without data). Status note: a new branch placed after the existing `error && !currentData` branch
   and **before** the `truncated` and `loaded === 0` branches:

   ```ts
   `已加载 ${loaded} 个代站，${n} 个流域加载失败：${ids.slice(0, 3).join('、')}${n > 3 ? ' 等' : ''}`
   ```

   - one failed: `已加载 71 个代站，1 个流域加载失败：basins_hlj`
   - three: `已加载 71 个代站，3 个流域加载失败：a、b、c`
   - four: `已加载 71 个代站，4 个流域加载失败：a、b、c 等`
   - one basin empty and another failed: `已加载 0 个代站，1 个流域加载失败：basins_hlj` (wins over
     "暂无可渲染气象代站").

   With no failed basin every existing note is unchanged, byte for byte. The new note hides a simultaneous
   cap-truncation or missing-identity note, as the existing precedence already does between those two.
3. A partial result is cached under its request key like a complete one (`data` set, `error: null`; no
   refetch loop — the hook returns on `matches && data`); turning the overlay off and on clears the store and
   reloads, as today.
4. The example count in the requirement text changes from "Heihe 1709" to "`basins_hlj_vbasins` 1314": after
   #2699 heihe lists 287 stations and no longer exceeds one page. Test data using 1709 is untouched.

## Must preserve

- Every existing test of `stores/__tests__/stationLayerData.test.ts` passes unchanged (none asserts that one
  failing context rejects a load in which another context succeeded).
- The fields consumers read today (`stations`, `stationBasinIds`, `total`, `totalKnown`, `loaded`,
  `truncated`) keep their meaning; `failedBasinIds` is `[]` on a full success.
- No backend, API type or map-primitive change.

## Out of scope

Retry/backoff; parallel loading (contexts stay serial: a slow basin still delays the layer, and an all-failed
load now reports after the last context instead of the first); per-basin error details in the UI beyond the
ids; the popup; the station MVT path.

## Evidence

vitest, store (`apps/frontend/src/stores/__tests__/stationLayerData.test.ts`):
- two contexts, the first rejects on its first page → resolves; stations of the second only;
  `failedBasinIds = [first]`, `truncated = true`, `totalKnown = false`, `total` = the second's count; the
  store has `data` and `error: null`.
- two contexts, one fails on its second page → it keeps its first page's stations, is listed as failed and
  `total` includes its `total_count`; the other loads fully.
- a resolved-source context whose latest-product lookup rejects → the station request is made with the
  basin-only identity, the basin is **not** in `failedBasinIds`, `truncated` stays false.
- every context rejects → the load rejects with the first error; store `data: null`, `error` set.
- full success → `failedBasinIds = []`.

vitest, hook — a **new** suite `apps/frontend/src/pages/m11/__tests__/useStationLayer.test.tsx` (there is
none today; follow the `renderHook` pattern of `useHydroMetProduct.test.tsx` in the same directory). It first
pins each existing note branch of `useStationLayer.ts` (~:111-124) as it is on master, then the new branch:
the four strings above, and `failedBasinIds` on the model.

Commands: `pnpm test`, `pnpm typecheck` (`tsc --noEmit` without `-p` checks nothing: the root tsconfig has
`files: []`), `pnpm build`.
