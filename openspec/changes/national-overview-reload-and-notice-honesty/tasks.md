## Risk Triage

```text
Issue type: bug batch (#2127 primary, #2131, #2139; #2103 verify-and-close)
Project profile: NHMS (openspec/project-profile.md)
Blast radius: medium
Fixture level: expanded
Repair intensity: medium
Upstream suggested level: absent (issue-scribe issues, sizes S/S/M/S)
Why:
- concurrency / ordering: request generation, in-flight enrichment writes and
  a new re-derive path inside one store action (#2127)
- (source, cycle, valid_time) identity honesty on the national discharge layer (#2131)
- error surfacing on the page notice chain, bound by a canonical scenario (#2139)
- kept at medium: browser-only, no backend/contract/file IO/auth change; every
  failure mode is assertable in vitest with the existing API mock harnesses
Selected risk packs:
- Concurrency / shared state / ordering
- Error handling / rollback / partial outputs
- Resource limits / large input / discovery (request amplification)
- Hydro-met time series / forcing windows (domain: triple identity)
OpenSpec change: national-overview-reload-and-notice-honesty (generated)
Evidence floor:
- openspec validate national-overview-reload-and-notice-honesty --strict --no-interactive
- openspec validate display-v2-national-timeline-precip-overlay --strict --no-interactive
- cd apps/frontend && pnpm exec tsc --noEmit -p tsconfig.app.json && pnpm check:types && pnpm test && pnpm build
```

## Risk Pack Selection

| Pack | Selected | Reason |
|---|---|---|
| Public API / CLI / script entry | not selected | No route or URL-key change; URL writes by the page are unchanged. |
| Config / project setup | not selected | No config or dependency change. |
| File IO / path safety / overwrite | not selected | Browser code only. |
| Schema / columns / units / field names | not selected | Additive override status + exported reason builder; no API schema change. |
| Auth / permissions / secrets | not selected | Untouched. |
| Concurrency / shared state / ordering | selected | Nonce/identity split, mutable current query read by late enrichment writes, re-derive while in flight. |
| Resource limits / large input / discovery | selected | The fix removes per-tick request amplification; tests assert zero requests. |
| Legacy compatibility / examples | not selected | Aged bookmarks keep rendering (D2); `m11-overview-empty` test id kept (D3). |
| Error handling / rollback / partial outputs | selected | `bootstrapError` surfacing; partial `error` must survive timeline steps; failed loads no longer retried per tick. |
| Release / packaging / dependency compatibility | not selected | None. |
| Documentation / migration notes | not selected | Spec deltas only; no runbook/doc surface. |
| Published NHMS artifacts / display identity (domain) | not selected | Tile URLs unchanged; overlay stays null for unavailable layers. |
| Hydro-met time series / forcing windows (domain) | selected | `(source, cycle)` membership and the disabled-reason ladder. |

## Must preserve

- Default load (`/`, default source, no `cycle`) issues zero valid-times requests and the same request set and order as today.
- Non-default source: cycles awaited before any valid-times / precipitation index request; no GFS default cycle on those requests (#2103 tests unchanged).
- Precipitation toggle issues no request (existing test `does not re-request anything when only the precipitation toggle changes`).
- The four existing disabled reasons, their ladder order, and `resolveM11NationalValidTimeCorrection` deferral on pending/error.
- Identity-changing queries (source, cycle, layer, version ids, segment, q) reload exactly as today, including stale-write fencing.
- Notice chain order: met-station status → loading → overview-empty → precipitation; one notice at a time.
- All existing assertions of `OverviewPagePrecipOverlay.test.tsx` except the two notice lines rewritten under D4.

## Seams under test

- `useOverviewDataStore.getState().loadOverview` with the `mockApi` harness of `src/stores/__tests__/overviewData*.test.ts` (request log + deferred responses).
- `normalizeLayerStates` / `buildM11RegisteredOverlay` / `deriveM11ControlBarModel` as pure functions.
- `OverviewPage` rendered with the harness of `src/pages/__tests__/OverviewPagePrecipOverlay.test.tsx`.

## Tasks

Implementation order: 2 (#2139) → 3 (#2131) → 4 (#2127). Reference by symbol.

### 1. Fixture

- [x] 1.1 Proposal, design, tasks, ADDED deltas for `overview-data-contracts` and `frontend-mvt-layer-consumption`.
- [x] 1.2 Fixture review (iteration 1 revise: 8 gaps; iteration 2 revise: handle replacement on identical-query reload + E9(b) setup, both applied verbatim as the reviewer's suggested wording — iteration cap reached, no third review); `openspec validate` strict for this change and display-v2.

### 2. Bootstrap failure notice (#2139)

- [x] 2.1 `pages/OverviewPage.tsx` `OverviewMode`: widen the `emptyBasinReason` gate per D3; update the notice-chain comment (bootstrap failure no longer depends on the basin count).
- [x] 2.2 Rewrite the two notice assertions of "surfaces the index-error notice when a bootstrap failure skips the layer-time enrichment chain" per D4 (E1).

### 3. Cycle-not-listed reason (#2131)

- [x] 3.1 `lib/m11/overviewDataContracts.ts`: new `ActiveCycleValidTimesOverride` status carrying source and cycle; exported reason builder; ladder branch placed so the four existing reasons are unchanged; not part of the unresolved (deferral) predicate.
- [x] 3.2 `stores/overviewData.ts` `buildLayerStates`: pass the new status per D2 (available empty list + available cycles record + non-member); pass `pending` when the cycles record is absent; otherwise unchanged.
- [x] 3.3 Tests E2–E5.

### 4. validTime-only re-derive (#2127)

- [x] 4.1 `stores/overviewData.ts`: request identity without `validTime`; module-level re-derive handle set by the active load and cleared by `clearOverviewDataCache`; short-circuit per D1 (same identity **and** different `validTime`) before any nonce bump or `set` of loading/error flags; identical queries keep today's path. Update the stale "validTime-only 重载" comment in `OverviewPage.tsx` `precipCatalog`.
- [x] 4.2 Per D1: held current query read by `layerStateInputs.query` (phase-1 `query` and phase-2 `concreteSurfaceQuery`), `normalizeOverviewSummary`, `overviewRequestScope`, `createEmptyOverviewSummary` (placeholder + catch fallback); held summary inputs; held `bootstrapSnapshot` re-timed (not rebuilt) and read by `finalSnapshot.bootstrap` and the catch fallback instead of the promise value; all validTime-dependent snapshot values computed in the sync block before each `set` (move phase-2 `summary` after `await bootstrapPromise`); load promise resolves to the store `overview` when still current.
- [x] 4.3 Tests E6–E10, E14.

### 5. Close-out

- [ ] 5.1 PR closes #2127, #2131, #2139, #2103; #2103 closing note per design D5 (AC4 superseded by #2014 decision 13, sub-case → #2140).
- [x] 5.2 E12 pre-deploy attempt recorded (no timeline on the deployed bundle); post-deploy capture recorded as follow-up if not deployed at merge time.
- [ ] 5.3 After merge: archive this change.

## Evidence Mapping

| ID | Seam | Input | Expected |
|---|---|---|---|
| E1 | `OverviewPage` precip harness | runless `/api/v1/layers` rejects once, retry succeeds, basins non-empty, URL with `validTime` | `bootstrapError` non-null; `m11-overview-empty` text === `bootstrapError`; `m11-precip-notice` absent; `data-precip-hidden-reason` === `index_error`; no `data-precip-url` |
| E2 | store + `normalizeLayerStates` | `?source=ifs&cycle=<C_gfs>`, IFS cycles healthy without `<C_gfs>`, `(ifs,C_gfs)` valid times `[]` | discharge unavailable; `disabledReason` contains `IFS` and `<C_gfs>`; differs from all four existing reasons; `buildM11RegisteredOverlay` null; `deriveM11ControlBarModel(...).cycle` equals the store's `activeNationalCycle` |
| E3 | same | `?source=gfs&cycle=<C_ifs-only>` and `?cycle=1999-01-01T00:00:00Z` | same cycle-not-listed reason family (names source + cycle) |
| E4 | same | listed cycle with `[]`; unlisted cycle with non-empty list | `'Layer has no valid times.'`; layer available with that list |
| E5 | store, deferred `/cycles` for default source | `?source=gfs&cycle=<C_unlisted>`, valid times `[]` resolved before cycles | `pendingActiveCycleValidTimesDisabledReason` until cycles resolve, then the cycle-not-listed reason |
| E6 | store, subscribe recorder | settled load of Q(T1) (default pair), then `loadOverview` Q(T2), Q(T3) | no request; recorder never sees `mapBootstrapLoading` or `enrichmentLoading` set to `true`, nor `error`/`bootstrapError` cleared; three maps and `layerTimeEnrichmentSkipped` unchanged. (Request count alone is not a red proof here: `cached()` already dedupes settled and in-flight values) |
| E7 | store, deferred cycles/valid-times/precip index (non-default source), subscribe recorder | Q(T1) in flight, then Q(T2) before responses | no loading flag re-set to `true` after the Q(T2) call; responses land in `cyclesBySource` / `validTimesByCycle` / `precipIndexByCycle`; discharge `currentValidTime` reflects T2 after they land |
| E8 | store request log | pipeline status rejects (partial `error` set), then validTime-only call | `error` unchanged; pipeline status request **not** re-sent (today the rejected cache entry is deleted and the reload re-sends it: the request-count red proof) |
| E9 | store equivalence, fixed clock, default pair | (a) settled Q(T1) then Q(T2); (b) Q(T1) with only the runless `/api/v1/layers` (no `run_id`) blocked and the mock returning at least one published run so phase 2's catalog uses the run-scoped key (phase 2 shares `fetchBasins()` and, without a run, the runless key with phase 1), phase 2 returned, then Q(T2), then release | `overview` deep-equal to `clearCache` + fresh load of Q(T2) (layers, bootstrap, requestScope, summary); `overviewSnapshotMatchesQuery(overview, Q(T2))` true; in (b) the promise returned by the Q(T1) call resolves to that overview; `buildM11RegisteredOverlay(Q(T2), layers)` tile URL has T2 as its valid_time segment |
| E10 | store | settled Q, then Q with a different `cycle` or `source` | a new request generation starts (loading flags set, requests issued as today) |
| E11 | red proof | pre-change tree | E1 red (notice null); E2/E3 red (`'Layer has no valid times.'`); E6/E7 red (loading flag re-set / error cleared); E8 red (pipeline re-sent); mutant: `writeValidTimes` rebuilds from a `layerStateInputs.query` that ignores the held current query → E7 red; mutant: short-circuit identical queries too → E14 red. Record each red run and revert |
| E12 | live display (node-27 C4 browser lane) | agent-browser on `https://test.nwm.ac.cn/` playback and `?source=ifs&cycle=<GFS-only cycle>` | pre-deploy attempt 2026-09-13 (`.workplans/issue-2127/evidence/e12-baseline.txt`): the deployed bundle predates #2125 and has no timeline, so no per-step baseline exists. Post-deploy capture: zero requests per timeline step (4x and 1x), cycle-not-listed text, and a ruling on the "loading notice flicker" open item of #2127. Deploy is an operator action; the capture is a follow-up when not deployed at merge |
| E13 | toolchain + regression | full frontend suite | `pnpm exec tsc --noEmit -p tsconfig.app.json && pnpm check:types && pnpm test && pnpm build` green; baseline 955 tests on `origin/master` 177e08dd7, no existing assertion removed or loosened except D4 |
| E14 | store request log | settled load of Q then `loadOverview` with an identical Q (remount); settled failed bootstrap then identical Q | a new request generation starts; the failed bootstrap request is retried; after the identical-query reload settles (mock returns a different pipeline `updated_at` on the second round), a Q(T2) call re-derives from the new generation's inputs (summary carries the second `updated_at`) |
