## Why

Two open frontend issues touch the same two files (`apps/frontend/src/stores/overviewData.ts`, `apps/frontend/src/lib/m11/overviewDataContracts.ts`); the owner asked for them in one PR (2026-09-13).

- **#2140** — one `loadOverview` generation resolves the national discharge `(source, cycle)` pair from two different catalog snapshots:
  - phase 3 writes `validTimesByCycle` / `precipIndexByCycle` keys from the phase-1 **runless** `/api/v1/layers` response;
  - `buildLayerStates` reads them through the phase-2 **merged** catalog, where the run-scoped discharge entry wins.
  - The backend computes `metadata.default_cycle` per call, so a cycle flip between the two fetches makes the keys differ and fails silently:
    - no URL `cycle`: the precipitation overlay stays `index_pending` with no notice;
    - URL `cycle=<old default>`: the discharge layer shows "still loading" while no request is in flight.
  - PR #2334 removed the incidental self-heal (a timeline step used to reload the whole overview), so the stuck state now lasts until an identity change, a remount or a page reload.
- **#2332** — the #2328 lane deletion kept a set of symbols that only tests, or nothing, still reference:
  - basin-river map primitives and constants, `M11RiverTooltip`, `buildBasinRiverFeatureCollection` and its types;
  - `BasinSegmentRow`, `m11BasinRiverLayerColor`, `numberOrZero`;
  - the `derivedValidTimes` input and `'derived'` valid-time source;
  - `basinRiverUnavailableReason`, `hasBasinRiverNetwork`, and the `basinId` parameter of `fetchRuns*`.
  - Two docs drift items from PR #2331's final review ride along.

## What Changes

- **Catalog identity (#2140):** while a generation's bootstrap succeeded, its national `discharge` catalog entry comes from the bootstrap (runless) snapshot only. `mergeLayerCatalogs` is not asked to let the run-scoped discharge entry replace it. The phase-3 writer and every reader then resolve the pair from one snapshot.
  - The active display-v2 delta already requires the run-scoped discharge entry to carry the same `default_source` / `default_cycle` / `valid_times` as the runless one (`openspec/changes/display-v2-national-timeline-precip-overlay/specs/overview-data-contracts/spec.md` "Run-scoped `/api/v1/layers?run_id=<X>` catalog"). The client now enforces that identity per generation instead of assuming it.
  - Only when the bootstrap catalog **has** a discharge entry: a runless catalog without one (no display-ready run yet at that instant) keeps today's behaviour.
- **Lane residue (#2332):** delete the symbols listed in #2332 and the test cases that exist only to pin them. Drop `basinId` from `fetchRuns` / `fetchRunsPage` / `fetchRunsPageByStatus`; the request query object is proven unchanged by test, the URL is unchanged because openapi-fetch's query serializer skips `undefined` values, and the cache key is unchanged because `JSON.stringify` drops them. Fix the two docs drift items.

## Non-goals

- Backend `default_cycle` computation or caching; the backend `/api/v1/runs` `basinId` parameter.
- A refetch-on-mismatch self-heal (#2140 alternative).
- Narrowing `buildSelectedSegmentFeatureCollection`'s always-null geometry path (#2332 leaves it optional; kept as is, see design D3).
- Splitting the large files (#2102).
- Post-deploy live receipt of PR #2334 (#2336).

## Impact

- Frontend only: store, contracts, map builders/primitives/runtime/surface, their tests; one pytest docstring; one runbook line reference.
- Specs: one ADDED requirement in `overview-data-contracts`. The canonical "Non-layer detail payload derives valid times" scenario is already dropped by the active display-v2 change's MODIFIED "Timeline is driven by valid times", so removing the `'derived'` source needs no new delta (design D4).
