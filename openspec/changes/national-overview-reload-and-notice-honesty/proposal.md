## Why

Four open display-v2 frontend defects share one surface — the national overview
store (`apps/frontend/src/stores/overviewData.ts`) and the notice chain of
`OverviewPage` — and the owner asked for them to land in one PR (2026-09-13):

- **#2127** — every timeline step re-enters the whole `loadOverview`
  orchestration. `dataIdentityQuery` normalises `precip` but not `validTime`,
  so each `validTime` write bumps `overviewRequestNonce`, clears
  `bootstrapError`/`error`, drops in-flight enrichment writes, and re-sends every
  currently failing endpoint on every playback tick (up to 4 Hz). No request in
  `loadOverview` is keyed by `validTime`; the reload is pure waste on the network
  and pure damage to state.
- **#2131** — a URL `cycle` that is not in the selected source's own cycle list
  (`?source=ifs&cycle=<GFS-only cycle>`, a fabricated cycle, or a bookmark whose
  coverage vanished) gets `200 + []` from valid-times and renders the discharge
  layer disabled with `'Layer has no valid times.'`. The source does have valid
  times, just not for that cycle; the text is false.
- **#2139** — when the map bootstrap fails but phase 2 recovers a non-empty basin
  list, `bootstrapError` has no render surface (its only surface,
  `emptyBasinReason`, is gated by `basins.length === 0`). The only visible text
  is the precipitation notice "降水索引加载失败", which blames the wrong
  subsystem and violates canonical `overview-data-contracts` "Map bootstrap
  rejection" (truthful bootstrap-failed state).
- **#2103** — `/?source=ifs` without `cycle` paired IFS with the GFS default
  cycle. The fix already landed with #2014 (`a2e866ae`, per-source default arm of
  `nationalDischargeActivePair`, cycles awaited for non-default sources) and its
  acceptance tests are on master. This change only verifies and closes it; see
  design D5.

## What Changes

- `loadOverview` treats a call whose query differs from the active request only
  in `validTime` as a **re-derivation**, not a reload: no nonce bump, no error
  clearing, no HTTP request; layer states, the snapshot request scope and the
  summary are re-derived from the inputs the active load already holds, and
  in-flight enrichment keeps landing and derives with the latest `validTime`.
- A new discharge disabled reason names the source and cycle when the active
  cycle is not listed for the source and its valid-times list came back empty;
  `'Layer has no valid times.'` stays reserved for a listed cycle with no
  coverage.
- `OverviewPage` renders `bootstrapError` whenever the surface has settled,
  independent of the basin count, keeping its place ahead of the precipitation
  notice.

## Non-goals

- Backend contract changes (rejecting uncovered `(source, cycle)` pairs with
  4xx — #2153).
- Correcting a non-member URL cycle to the source default (#2131 option A): it
  would rewrite aged bookmarks that still render today, and force the default
  source arm to await `/cycles` (design D2).
- Runless vs run-scoped catalog `default_cycle` flipping inside one load
  (#2103 comment sub-case b) — tracked by #2140.
- Debouncing timeline writes, control-bar layout (#2128), API envelope runtime
  validation (#2129), splitting `overviewData.ts` (#2102).
- Changing the text of `bootstrapError` itself or the `m11-overview-empty`
  test id (consumed by the C4 display-evidence tooling).

## Impact

- Frontend only: `apps/frontend/src/stores/overviewData.ts`,
  `apps/frontend/src/lib/m11/overviewDataContracts.ts`,
  `apps/frontend/src/pages/OverviewPage.tsx`, their vitest suites.
- Specs: ADDED requirements in `overview-data-contracts` and
  `frontend-mvt-layer-consumption` (new headers only; the display-v2 change
  still MODIFIES neighbouring headers, see design D6).
