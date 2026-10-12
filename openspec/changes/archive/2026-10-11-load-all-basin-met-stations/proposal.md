# Load met stations for every basin, with bounded concurrency

Issue: #2867 (ruling 2026-10-11: candidate C without hard caps). Fixture level: **compact**.
Risk packs: frontend-display (read-only), request fan-out.

## Why

The station overlay on `/` fetches basins serially in basin-name order and stops once 5000
stations have accumulated (`STATION_CLIENT_CAP`), and never requests more than the first 50
basin contexts (`STATION_CONTEXT_CAP`). Both numbers were written when two basins had
stations. With 66 basins / ~14.5k stations online (measured 2026-10-10), 45 basins have no
station feature at all, and the status note only says "列表已截断".

## What Changes

- `apps/frontend/src/stores/stationLayerData.ts`
  - Every normalized basin context is fetched. Basins are loaded by a worker pool of
    `STATION_FETCH_CONCURRENCY = 6`; workers claim contexts in context order.
  - The merged result is assembled in **context order**, independent of completion order:
    `stations`, `stationBasinIds`, `failedBasinIds`, and the error thrown when every
    attempted context fails (the first failed context in context order).
  - The two caps stay only as safety valves far above current volume:
    `STATION_CONTEXT_CAP = 500`, `STATION_CLIENT_CAP = 50_000`. A worker does not start a
    new context once the stations already fetched reach the client cap; the merge cuts at
    the cap. Within one basin, paging always asks for `STATION_PAGE_LIMIT` and stops when
    that basin's own rows reach the cap.
  - Valve semantics are deterministic (independent of scheduling). Merge walks the
    contexts in order; the **cut context** is the one at which the merged count reaches
    `STATION_CLIENT_CAP`. Everything fetched for contexts after the cut context is
    discarded: those contexts add nothing to `stations`, `total` or `failedBasinIds`.
    `unloadedBasinCount` = (contexts after the cut context) + (contexts beyond
    `STATION_CONTEXT_CAP`). It counts contexts, whether or not their inventory would have
    been empty. `total` = sum of `total_count` over the contexts up to and including the cut
    context that returned a first page (a basin failing on a later page keeps its
    `total_count` and the pages it got, as before; one failing on its first page adds 0). When `unloadedBasinCount > 0`: `truncated = true`,
    `totalKnown = false`.
  - When the cap is reached with no context after the cut (one basin larger than the cap,
    or the sum landing exactly on it), `unloadedBasinCount = 0` and the existing
    `loaded < total` / mid-basin truncation marking applies unchanged.
  - The valve bounds the merged result, not network volume: up to
    `STATION_FETCH_CONCURRENCY` in-flight basins may each page to the cap before the cut is
    known. Acceptable for a valve far above current volume.
  - A superseded load stops: `fetchAllStations` takes an `isCurrent()` predicate (the
    store passes `nonce === requestNonce && activeRequestKey === key`); workers check it
    before claiming a context, before a basin's first station page (after its
    latest-product lookup) and before each further page; a load that finds itself
    superseded at one of those checkpoints rejects with `Error('代站图层请求已被取代')`.
    A load that passes no further checkpoint after being superseded still resolves, and the
    store does not adopt its data. The store's existing catch
    already leaves state alone for a non-current nonce. Reason: the request key includes
    source and cycle, so cold start (source null → GFS/IFS) and every cycle switch start a
    new full load; without this the old load's workers keep running and real concurrency is
    a multiple of the bound.
  - Per-basin behaviour is unchanged: latest-product lookup when the source is resolved,
    fallback to basin-only identity when that lookup fails, failure isolation (#2694),
    "all attempted contexts failed" throws.
  - The comment that calls the client cap "单流域" is corrected.
- `apps/frontend/src/pages/m11/useStationLayer.ts`
  - Exposes `unloadedBasinCount`.
  - Status note: with failed basins the existing sentence is kept and, when
    `unloadedBasinCount > 0`, `；另有 N 个流域未加载` is appended. Without failed basins and
    with `unloadedBasinCount > 0`: `已加载 X 个代站，N 个流域未加载（超出客户端上限）`.
    The two existing truncation sentences remain for the residual case (a single basin
    cut mid-way, or `loaded < total`).
- Tests in `stores/__tests__/stationLayerData.test.ts` and
  `pages/m11/__tests__/useStationLayer.test.tsx` follow.
- Docs that describe the 5000 / 50 truncation as current behaviour are corrected:
  - `docs/runbooks/display-mobile-evidence.md` :28, :31, :139, :142, :177 (the 50 / 5000
    truncation, "pick a basin early in name order", `STATION_HOOK_NOT_RENDERED` attributed
    to truncation, "at most 50 basins serially").
  - `docs/runbooks/display-mobile-real-device-checklist.md` :120 (RD-07): with a full load
    there is no status note any more. The step is rewritten to check the transient
    `气象代站加载中` note against the controls while the overlay loads, and to be recorded
    as not applicable when the note has already gone.
  - Dated receipts are history and stay.

## Must preserve

- `OverviewPage.tsx` context construction, the store's request-key / nonce / in-flight
  dedup logic, `overviewData` store, the cluster policy, click resolution and the station
  popup are not touched.
- No API contract change; the server-side page clamp (200) is left as is.
- Request-key computation and the in-flight dedup map are unchanged; the only change to
  supersession is the stop described above.

## Out of scope

- Same-coordinate stations from different basins (split into its own issue).
- Viewport-driven loading (candidate A) and a national endpoint / MVT (candidate B).

## Evidence floor

- vitest, red-before-green or mutation-proven:
  - more than 50 contexts and more than 5000 total stations: every basin, including the
    last one, has features; `truncated = false`, `unloadedBasinCount = 0`;
  - concurrency: within one load, peak in-flight basin loads is > 1 and
    ≤ `STATION_FETCH_CONCURRENCY`;
  - supersession: after a second load with a different key starts, the first load claims
    no further context and requests no further page, and rejects; store state reflects
    only the second load;
  - result order equals context order when later contexts resolve first;
  - failure isolation and "all failed throws" still hold under concurrency; `failedBasinIds`
    in context order;
  - valves, driven by controlled resolvers (not microtask order): client cap tripped with
    contexts after the cut → `loaded = STATION_CLIENT_CAP`, `unloadedBasinCount` and
    `total` per the rule above regardless of completion order, status note names the count;
    cap reached with nothing after the cut → `unloadedBasinCount = 0`; context cap tripped
    → count of the excess contexts;
  - the existing tests whose meaning changes are rewritten, not deleted
    (`stationLayerData.test.ts` :133-147, :168-191, :193-213, :508-512;
    `useStationLayer.test.tsx` :119-131, :133-143, :240-249);
  - latest-product fallback unchanged.
- `pnpm test`, `pnpm typecheck`, `pnpm run check:types`, `pnpm build`; full mocked
  Playwright lane (station overlay specs must not change meaning).
- node-27 live receipt (PR build via the verify checkout's preview port against the live
  API, production deploy untouched), measured with Playwright request / response events on
  a cold load of `/?metStations=1`: basin count, station total, number of `/met/stations`
  and latest-product requests, number of loads started (key changes), global peak
  concurrent station requests, HTTP protocol, wall time to the last station response and
  to a stable `data-met-station-feature-count`, that final feature count, final status
  note; a basin late in name order (e.g. `basins_wj`) has stations; desktop oracle still
  passes.
