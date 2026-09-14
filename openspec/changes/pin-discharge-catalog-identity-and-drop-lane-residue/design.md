## Context

`loadOverview` (`apps/frontend/src/stores/overviewData.ts`) runs three phases per generation:
- Phase 1 fetches `fetchLayers(null)` (runless).
- Phase 2 fetches `fetchLayers(latestRun?.run_id)` and builds `layers = mergeLayerCatalogs(bootstrap.layers, scopedLayers)`, where a scoped entry replaces the runless one unless the runless one is time-less.
- Phase 3 awaits bootstrap and resolves `nationalDischargeActivePair(query, snapshot.layers, cyclesBySource)` from the **runless** snapshot, then writes per-cycle keys and decides `isDefault` (no valid-times request for the default pair).

`buildLayerStates` resolves the same pair from `layerStateInputs.layers`: the runless catalog after phase 1, the merged catalog after phase 2. The two HTTP responses are separate `cached()` entries (60 s TTL) and the backend computes `default_cycle` per call, so the two snapshots can disagree on the discharge `default_cycle` (and therefore `valid_times`).

## Decisions

### D1: one discharge identity per generation (#2140)

- When the generation's bootstrap snapshot exists **and contains a `discharge` entry**, the merged catalog's `discharge` entry is that bootstrap entry. Implementation: phase 2 removes `discharge` from the scoped catalog before `mergeLayerCatalogs` only in that case. `mergeLayerCatalogs` itself stays unchanged (pure contract helper with its own tests and other layer ids).
- A bootstrap catalog **without** a discharge entry (the runless `/api/v1/layers` returns `[]` when no run is display-ready at that instant, while a run-scoped fetch moments later can carry discharge; the backend catalog cache is keyed per `run_id`) keeps today's behaviour: the scoped entry is used, otherwise the whole generation would have no discharge layer. Known residual, not fixed here: in that window phase 3 resolved no pair, so precipitation can still sit in `index_pending` until the next load — the same class as #2140, reachable only at first-run-becomes-ready.
- A fail-closed (time-less) runless discharge entry is already kept over the scoped one by `mergeLayerCatalogs`' time-less rule; this change is a no-op for it.
- When bootstrap failed (`bootstrapForSnapshot === null`), phase 3 issues nothing (`markLayerTimeEnrichmentSkipped`), so the scoped catalog is the only snapshot and stays as today.
- Every pair resolver in the generation then sees the same `default_cycle` / `default_source` / `valid_times`:
  - the phase-3 writer (`snapshot.layers`);
  - `buildLayerStates` after phase 1 and after phase 2;
  - `writeCycles` / `writeValidTimes` / `markLayerTimeEnrichmentSkipped` recomputes;
  - the #2334 re-derive path.
- MapLibre source identity becomes stronger than the contract: bootstrap and final snapshots now read the same discharge entry, so on a flip the metadata `version` (digest over `(default_source, default_cycle)`) no longer jumps between the two snapshots of one generation. "Frontend enrichment phase does not downgrade discharge" still holds.
- Staleness trade-off: the displayed default cycle can be up to one runless TTL (≤60 s) older than the scoped response. Today either snapshot can be the older one, so no freshness guarantee is lost.
- Rejected: resolving the writer from the merged catalog (would move all layer-time requests behind phase 2's runs → scoped-layers chain, delaying the precipitation index and valid times and touching "Cycles and precipitation index requests stay off the bootstrap critical path" timing); refetch-on-mismatch (extra request + nonce interplay).

### D2: lane residue deletion (#2332)

- Delete exactly the #2332 symbol list, the test cases that only pin them, and the `m11-basin-river-unavailable` notice branch of `m11MapRuntime.tsx` (its only source is the removed prop).
- `LayerState['validTimeSource']` becomes `'api' | 'none'`; `normalizeLayerStates` loses `derivedValidTimes`. With it absent, `validTimes` is `apiValidTimes`, byte-identical to today, because production never passed `derivedValidTimes`.
- `fetchRuns(query)` / `fetchRunsPage(query, limit, offset)` / `fetchRunsPageByStatus(query, limit, offset, status)` lose `basinId`. Cache key unchanged (`JSON.stringify` drops `undefined`); the `mockApi` log records the pre-serialization `params.query`, so the test proves query-object equality, and URL equality follows from openapi-fetch skipping `undefined` query values. The existing `allowedRunQueryKeys` assertion that still lists `'basin_id'` stays untouched (Must preserve).
- Docs drift:
  - the `tests/test_hydro_display_mvt_scaling.py` docstring stops citing the deleted `fetchLayerValidTimes` fallback;
  - the `overviewData.ts` phase-1 comment stops naming `fetchLayerValidTimes`;
  - the `pages/m11/M11Controls.tsx` comment that says `buildLayerStates` passes neither `validTimesByLayerId` nor `derivedValidTimes` drops the deleted name;
  - `docs/runbooks/current-production-ops.md` replaces the `stores/overviewData.ts:537` line reference with a symbol reference.

### D3: not narrowing the selected-segment geometry path

`buildSelectedSegmentFeatureCollection` keeps its always-null `geometry` parameter and budget path. It still feeds `unavailableReason` in production, and narrowing it is a behaviour-adjacent refactor with no defect attached. Recorded as a choice, as #2332 requires.

### D4: spec placement

- #2140 adds one requirement to `overview-data-contracts` (new header, archive-order safe against display-v2's MODIFIED headers).
- #2332 needs no delta:
  - canonical `map-layer-timeline-controls` "Timeline is driven by valid times" still carries the "Non-layer detail payload derives valid times" scenario, but the active display-v2 change MODIFIES that requirement without it (basin detail and segment detail are retired);
  - no remaining surface has a non-layer detail payload (`/segments/:segmentId` redirects to `/`).
  - Removing the `'derived'` arm is consistent with the spec as display-v2 archives it; recorded here rather than re-deleted.
