## Context

`useOverviewDataStore.loadOverview(query)` runs three phases: bootstrap
(basins + runless catalog), enrichment (models, runs, queue, scoped catalog,
pipeline, versions) and layer-time enrichment (per-source cycles, per-cycle
valid times, precipitation index). Every store write is fenced by
`isCurrentRequest()` (`requestNonce` + `activeOverviewRequestKey`). Layer states
are derived by `buildLayerStates` from `layerStateInputs` and re-derived in
place by `writeCycles` / `writeValidTimes` / `markLayerTimeEnrichmentSkipped`.

`OverviewPage` calls `loadOverview(dataLoadState)` whenever `dataLoadState`
changes; `dataLoadState` includes `validTime`, and the bottom control bar's
playback and slider write `validTime` at up to 4 Hz.

`validTime` has exactly three consumers inside a load, none of them a request:
`normalizeLayerStates` (`pickCurrentValidTime`, layer freshness),
`normalizeOverviewSummary` (`latestUpdate`, freshness, `sourceSelection`
provenance) and `overviewRequestScope` (`dataKey`, `validTime`).

## Decisions

### D1 — validTime-only calls re-derive, they never reload (#2127)

- Request identity = the data identity query with `validTime` set to `null`
  (the same split `requestScopeQueryKey` / `requestScopeDataKey` already make).
  `activeOverviewRequestKey` holds that identity.
- The active load exposes a module-level re-derive handle (cleared by
  `clearOverviewDataCache`, replaced by **every** new request generation —
  including a reload triggered by an identical query) and holds its current
  query. Writes made through the handle are fenced by that generation's
  `isCurrentRequest()` like every other write of the load. The short-circuit applies when the call's identity
  equals `activeOverviewRequestKey`, the handle is set, and the call's
  `validTime` **differs** from the held current query's `validTime` — whether
  the load is in flight or settled. A call with an **identical** query keeps
  today's behaviour: joins the in-flight load, or starts a fresh load when
  settled (so a remount of `OverviewPage`, e.g. returning from `/ops`, still
  refreshes expired data and retries a failed bootstrap).
- On the short-circuit the store MUST:
  - replace the held current query. Every validTime-reading derive site reads
    it: `layerStateInputs.query` (both the phase-1 `query` and the phase-2
    `concreteSurfaceQuery`, whose `validTime` is swapped while its concrete
    source is kept), `normalizeOverviewSummary`, `overviewRequestScope`,
    `createEmptyOverviewSummary` (placeholder and catch fallback).
    `nationalDischargeActivePair(query, …)` does not read `validTime` and may
    keep the outer `query`;
  - re-derive what the store already holds, from held inputs only:
    - `overview.layers` via `buildLayerStates` on `layerStateInputs` with the
      current query (the same edge `writeValidTimes` uses);
    - `overview.bootstrap.layerStates` / `currentLayerValidTime` by re-applying
      the new `validTime` to the held bootstrap layer states (same
      `pickCurrentValidTime` and freshness rule as `normalizeLayerStates`),
      **not** by rebuilding them from the live cycles / valid-times maps — the
      bootstrap snapshot stays frozen at bootstrap time except for `validTime`.
      The held `bootstrapSnapshot` variable is replaced, and both
      `finalSnapshot.bootstrap` and the catch fallback read that variable, never
      the `bootstrapPromise` resolution value;
    - `overview.requestScope`, and `overview.summary` from held summary inputs
      (pipeline, queue, latest run, runs, partial errors, basins);
  - leave `overviewRequestNonce`, `mapBootstrapLoading`, `enrichmentLoading`,
    `bootstrapError`, `error`, `layerTimeEnrichmentSkipped`, `cyclesBySource`,
    `validTimesByCycle`, `precipIndexByCycle` untouched and issue no request;
  - return the in-flight load promise, or a resolved promise of the current
    `overview` when settled.
- Ordering inside the load: every validTime-dependent value of a snapshot
  (layer states, summary, request scope, bootstrap) is computed in the
  synchronous block immediately before its `set`, after the last `await`
  (phase 2 currently computes `summary` before `await bootstrapPromise`; that
  must move). The load promise resolves to the store's `overview` at settle when
  the request is still current (so an in-flight caller sees the latest
  `validTime`), otherwise to the snapshot it built.
- Oracle: for the default pair, after validTime-only calls the store `overview`
  is deep-equal (fixed clock — `isStale` reads `Date.now()`) to a fresh load of
  the final query from the same responses, both settled and when the call lands
  while phase 2 has returned and bootstrap is still blocked.
- Consequence recorded, not a regression: a settled failed load is no longer
  retried by timeline steps; retry needs an identity change, a remount or a page
  reload. That retry-by-tick is exactly the request storm #2127 reports.
- Rejected: debouncing `validTime` in `OverviewPage` (lowers frequency, keeps
  error clearing and enrichment invalidation, adds a timing constant).

### D2 — honest reason for a cycle the source does not list (#2131, option B)

- New `ActiveCycleValidTimesOverride` status for the store to pass when the
  active pair is non-default, its valid-times record is `available` with an
  empty list, the source's cycles record is `available`, and `pair.cycle`
  (seconds precision) is not among `cycles.cycles[].cycle_time`.
- New exported reason builder naming the source (upper-case) and the cycle,
  unequal to `'Layer has no valid times.'`, `failClosedDischargeDisabledReason`,
  `pendingActiveCycleValidTimesDisabledReason` and
  `activeCycleValidTimesErrorDisabledReason`. It is a terminal state (validTime
  correction proceeds, like fail-closed).
- Empty list + source cycles record still absent (the default source does not
  await `/cycles`) → `pending` until `writeCycles` re-derives; membership is not
  yet knowable and `pending` is the honest text.
- Empty list + listed cycle, or + cycles record `error` → unchanged
  `'Layer has no valid times.'` (a real coverage gap / unknowable). The `error`
  arm is reachable only for the default source: a non-default source with a
  cycles `error` never resolves a pair and never requests valid times.
- Membership reads `cycles.cycles` only when it is an array (the envelope is an
  unchecked `as T`, same guard as `deriveM11ControlBarModel`); a malformed list
  counts as "membership unknown" and keeps `'Layer has no valid times.'`.
- Non-listed cycle with a **non-empty** list (aged bookmark whose coverage still
  exists) renders exactly as today.
- Requests are unchanged: the `(source, cycle)` valid-times and precipitation
  index requests are still sent once; `buildM11RegisteredOverlay` returns null
  because the layer is unavailable. The control bar keeps showing the URL cycle
  (the store's active cycle), so bar and map agree.
- Rejected: option A (fall back to the source default). Membership in `cycles[]`
  is bounded by the 12-day lookback while valid times are not, so it would
  rewrite working aged links, and the default-source arm would have to await
  `/cycles`, reopening the "zero valid-times requests on default load" contract.

### D3 — bootstrap failure has a render surface independent of basins (#2139)

- `emptyBasinReason` gate becomes `!surfaceSettling && (basins.length === 0 ||
  bootstrapError !== null)`; its value order (`bootstrapError ?? error ?? …`) is
  unchanged. The notice chain order is unchanged, so a bootstrap failure still
  outranks the precipitation notice; the chain stays mutually exclusive.
- Kept test id `m11-overview-empty` (read by `src/lib/c4DisplayEvidence/dom.ts`
  and `src/test/c4DisplayFakePage.ts`); the name/meaning mismatch is accepted
  over breaking the C4 evidence tooling.
- `error` (enrichment partial error) keeps its basin-count gate: canonical spec
  asks for it in the affected panel only.
- Rejected: a separate notice with a new test id (breaks C4 tooling, and two
  absolutely positioned notices overlap).

### D4 — test that pinned the defect is rewritten, not weakened

`OverviewPagePrecipOverlay.test.tsx` "surfaces the index-error notice when a
bootstrap failure skips the layer-time enrichment chain" keeps its preconditions
and precipitation honesty oracles (`data-precip-hidden-reason === 'index_error'`,
no `data-precip-url`, faces agree) and flips the notice assertions:
`m11-overview-empty` text equals `bootstrapError`; `m11-precip-notice` absent.

### D5 — #2103 closes on existing evidence

Master already satisfies #2103 AC1–AC3 (`overviewDataSourceSelection.test.ts`:
pending with no GFS-cycle request, recompute to the IFS default cycle, tile URL
uses the IFS cycle). AC4 asked for a source-naming text when IFS cycles are
empty; #2014 decision 13 later ruled that an arrived empty per-source list is
the same fail-closed fact as the catalog one and shares
`failClosedDischargeDisabledReason`. That later decision stands; AC4 is recorded
as superseded (its literal wording — a source-naming text — is not met, and the
closing note says so), not re-implemented. The comment sub-case (runless vs run-scoped
`default_cycle` inside one load) is #2140.

### D6 — spec placement

`overview-data-contracts` "Map interactivity is decoupled from enrichment
loading" is MODIFIED by the active display-v2 change, so this change only ADDS
new requirement headers (archive-order safe). The `loadBasinDetail` /
`fetchLineage` acceptance item of #2127 is moot: that lane was deleted by #2328.
