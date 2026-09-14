## Risk Triage

```text
Issue type: refactor (dead-lane deletion) + spec retirement
Project profile: NHMS (openspec/project-profile.md)
Blast radius: medium
Fixture level: expanded
Repair intensity: medium
Upstream suggested level: absent (issues hand-written from the #2109 decision)
Why:
- routing / legacy compatibility: `/basins/:basinId` redirect and `basinId` query key change shape
- shared helper behavior: `M11MapLibreSurface` props, `resolveM11*ValidTimeCorrection`, `M11Timeline`
- 12 canonical specs + two active changes' deltas edited (display-v2, m11-popup)
- kept at medium: browser-only, no file IO, auth, persistence, publish or rollback surface;
  every failure mode is assertable in vitest; national render path is not modified
Selected risk packs:
- Public API / CLI / script entry (routes and URL query contract)
- Schema / columns / units / field names (M11QueryState shape)
- Legacy compatibility / examples
- Documentation / migration notes
OpenSpec change: retire-basin-detail-lane (generated)
Evidence floor:
- openspec validate retire-basin-detail-lane --strict --no-interactive
- openspec validate display-v2-national-timeline-precip-overlay --strict --no-interactive
- openspec validate m11-popup-station-overlay-usability --strict --no-interactive
- cd apps/frontend && pnpm exec tsc --noEmit -p tsconfig.app.json && pnpm check:types && pnpm test && pnpm build
```

## Risk Pack Selection

| Pack | Selected | Reason |
|---|---|---|
| Public API / CLI / script entry | selected | Browser routes `/basins/:basinId`, `/` query contract. |
| Config / project setup | not selected | No config, env, build or dependency change. |
| File IO / path safety / overwrite | not selected | Browser code only. |
| Schema / columns / units / field names | selected | `M11QueryState.basinId` removed; store state slices removed. |
| Auth / permissions / secrets | not selected | No RBAC/auth surface touched (`M11OpsLink` unchanged). |
| Concurrency / shared state / ordering | not selected | The deleted strip gate guarded a race that only exists while `BasinDetailMode` can mount; no remaining ordering logic changes. |
| Resource limits / large input / discovery | not selected | Deletion only; fewer requests, no new fetch. |
| Legacy compatibility / examples | selected | Old `/basins/*` and `?basinId=` links must keep landing on `/`. |
| Error handling / rollback / partial outputs | not selected | Removed error slices had no reachable renderer; national error chain untouched. |
| Release / packaging / dependency compatibility | not selected | No dependency change; bundle only shrinks. |
| Documentation / migration notes | selected | Spec deltas, display-v2 supersession notes, stale e2e evidence markdown. |
| Published NHMS artifacts / display identity (domain) | not selected | National tile/popup identity unchanged. |
| Hydro-met time series / forcing windows (domain) | not selected | Layer-derived valid-time path unchanged; only the unreachable payload-derived arm goes. |

## Must preserve

- `/`, every national overview behaviour and its tests' assertions: discharge MVT overlay, river-segment popup, met-station popup, basin click camera fit, bottom control bar, precip overlay and notice chain, national `validTime` correction gate.
- `/overview`, `/hydro-met`, `/meteorology`, `/forecast`, `/segments/:segmentId` redirects byte for byte.
- `segmentId`, `basinVersionId`, `riverNetworkVersionId`, `source` (`best`/`compare` still parse) query keys.
- `/monitoring`, `/system/model-assets`, `stores/forecast.ts` `fetchForecast`, `stores/modelAssets.ts` `fetchModels` (separate modules; not the store-local helpers).

## Seams under test

- `AppRoutes` rendered in `MemoryRouter` with `OverviewPage` stubbed (route → final location).
- `OverviewPage` mounted with a URL (location after normalisation + national DOM markers).
- `useOverviewDataStore` public state shape (no basin slices / action).

## Tasks

### 1. Spec retirement (spec PR, issue #2327)

- [x] 1.1 Proposal, design, tasks for `retire-basin-detail-lane`.
- [x] 1.2 Deltas: REMOVE all six requirements of `basin-drilldown-page` and ADD one "Basin drill-down page is retired" requirement (a capability with zero requirements fails `openspec archive`); REMOVE two and rewrite one requirement of `inplace-overview-basin-detail`; basin-detail clauses out of `frontend-navigation-state` (3 reqs), `frontend-visual-conformance` (3 reqs), `overview-data-contracts` (3 reqs), `map-layer-timeline-controls` (basemap), `met-station-cluster-layer`, `national-overview-page` (2 reqs), `responsive-screenshot-evidence`, `segment-detail-route-state`, `single-map-shell-routing`.
- [x] 1.3 Requirements also MODIFIED/ADDED by active `display-v2-national-timeline-precip-overlay` are edited in that change's deltas instead (design D4): `map-layer-timeline-controls` source controls + timeline + "Floating controls clear the control bar", `frontend-mvt-layer-consumption` basin-detail scenario, `precipitation-raster-overlay` `best`/`compare` wording, `frontend-visual-conformance` UI tokens, `map-first-layout-conformance`.
- [x] 1.4 `m11-popup-station-overlay-usability` (un-archivable before this change, design D4): the same basin-detail edits applied inside its `frontend-navigation-state`, `single-map-shell-routing`, `met-station-cluster-layer` deltas; its archive defects routed to #2326.
- [x] 1.5 Supersession notes appended to display-v2 `design.md` (D8) and `tasks.md` (`### #2109 裁决 B 取代记录`, tasks 6.5 and 6.6 closed with the reason).
- [x] 1.6 `openspec validate` strict for all three changes; archive-order check on scratch copies of `openspec/`: archiving `retire-basin-detail-lane` and `display-v2-national-timeline-precip-overlay` in both orders ends with identical canonical specs, all strict-valid; `m11-popup-station-overlay-usability` fails to archive in every order for its own pre-existing header mismatches (also on unmodified `origin/master`).

### 2. Frontend deletion (frontend PR, issue #2328)

Reference by symbol, not line number.

- [x] 2.1 `App.tsx`: extract the `<Routes>` table into an exported `AppRoutes` component that `App` renders inside `BrowserRouter` + `AppShell` + `Suspense` (no behaviour change); `/basins/:basinId` → `<LegacyRedirect />` without `param`; update the `LegacyRedirect` doc comment (`param` now maps only `segmentId`).
- [x] 2.2 `lib/m11/queryState.ts`: remove `basinId` (type, default, parse, serialize). The mechanical `basinId: null` removal in the `M11QueryState` literal of `components/map/__tests__/M11MapLibreSurfaceHook.test.tsx` is an allowed edit under E5. `basinId` arguments of hydroMet APIs (`loadHydroMetBootstrap`, `useHydroMetProduct`, popups) are a different field and stay untouched.
- [x] 2.3 `pages/OverviewPage.tsx`: remove the strip gate (`initialBasinStripRef`), `BasinDetailMode`, the mode switch, `basinId: null` in `OverviewMode`'s load query, basin-detail-only `M11FullscreenMap` props and their doc comments, and the stale「未选流域时诚实提示「请选择流域」」comment.
- [x] 2.4 `components/m11/BasinDetailPanels.tsx`: remove `useBasinDetailMode` and `basinDetailToOverviewBasin`; keep `bboxToMapFit`, `mapFeatureStringProperty`, `popupAnchorFromInteraction` (used by `OverviewMode`); relocate them only if the file would hold nothing else.
- [x] 2.5 `stores/overviewData.ts` + `lib/m11/overviewDataContracts.ts`: remove `loadBasinDetail`, `basinDetail`/`basinLoading`/`basinError`, and every helper/type that loses its last caller — at least `fetchLineage`, `fetchRiverSegment`, `fetchRiverSegments` (+ page helpers), `fetchRunsForBasinVersion`, `basinSnapshotMatchesQuery`, `basinSnapshotMetadataMatchesQuery`, `BasinDataSnapshot`, `normalizeSelectedSegmentDetail`, `COMPARE_LINEAGE_UNAVAILABLE` — each grep-confirmed (no `noUnusedLocals`).
- [x] 2.6 `components/map/*`: remove `basinSegments` / `selectedSegmentGeometry` / `popup` slot / `basin-river-segments` paths from `M11MapLibreSurface`, `m11MapInteractions.ts` and builders only where `OverviewMode` never supplies them; remove `M11BackToOverviewButton` and its test cases; `M11RiverForecastPanel` / `M11StationForcingPopup` untouched except the stale `basinSegments` mention in `M11RiverForecastPanel`'s doc comment.
- [x] 2.7 `pages/m11/M11Controls.tsx`: delete `M11MapSurface` (zero callers; forwards the props removed in 2.6); remove the `derivedTimes` arm: `M11TimelineDerivedTimes`, the `derivedTimes` prop of `M11Timeline`, the positional `derivedTimes` parameter of `buildM11TimelineViewModel`, the `derivedTimes` parameter of `resolveM11ValidTimeCorrection` / `resolveM11NationalValidTimeCorrection`, and the `'derived'` value of `validTimeSourceLabel`; the layer-derived path stays byte-identical (design D3). The display-v2 non-goal「`M11Controls.tsx` 死代码清理（report-only）」does not bind this change.
- [x] 2.8 Tests: delete `components/m11/__tests__/BasinDetailPanels.test.tsx`, `components/m11/__tests__/BasinDetailValidTimeCorrection.test.tsx`, `stores/__tests__/overviewDataBasinDetail.test.ts`. Strip basin-detail cases/fixtures from `pages/__tests__/OverviewPageValidTimeCorrection.test.tsx`, `stores/__tests__/overviewDataSourceSelection.test.ts`, `test/overviewDataFixture.ts`, `components/map/__tests__/M11FloatingControls.test.tsx`, `lib/__tests__/m11OverviewDataContracts.test.ts`. In `lib/__tests__/m11QueryState.test.ts`: drop `basinId` from the round-trip fixture, delete the #338 `basinId` boundary case, and repoint the `m11QueryHref('/basins/…')` pin to a non-basin path. No national assertion is removed or loosened.
- [x] 2.9 New pins E1–E3.
- [x] 2.10 `apps/frontend/e2e/m11-visual-evidence.md`, `e2e/m15-visual-evidence.md`: mark the `/basins/...` rows as retired historical evidence (#2109); do not delete provenance.
- [x] 2.11 After merge: close #2110 and #2132 as superseded; comment on #2039 that its frontend half is gone; archive this change (display-v2 and m11-popup stay active).

## Evidence Mapping

| ID | Seam | Input | Expected |
|---|---|---|---|
| E1 | `AppRoutes` in `MemoryRouter`, `@/pages/OverviewPage` replaced by a `vi.mock` stub that renders `useLocation()` pathname + search | `/basins/basins_qhh?source=ifs&validTime=2026-05-18T06:00:00Z` | stub shows pathname `/`, search keeps `source=ifs` and `validTime`, contains no `basinId` |
| E2 | real `OverviewPage` under `createMemoryRouter` + `RouterProvider` with the API mock harness of `pages/__tests__/OverviewPageValidTimeCorrection.test.tsx` | `/?basinId=basins_qhh&layer=discharge` | location search contains no `basinId`; `m11-fullscreen-map` has aria-label `全国总览地图`; no `m11-back-to-overview` element |
| E3 | store shape | `useOverviewDataStore.getState()` | no `loadBasinDetail`, `basinDetail`, `basinLoading`, `basinError` keys |
| E4 | grep oracle | command below | no output |
| E5 | national regression | existing national tests (`M11Shell`, `OverviewPage*`, `M11MapLibreSurface*`, `M11BottomControlBar`, precip, popups, `m11QueryState`) | pass; their diffs contain only basin-detail case deletions and the mechanical `basinId` literal removals listed in 2.2/2.8 |
| E6 | red proof | pre-change tree and post-change mutants | E3 red on the pre-change store; E4 prints matches on the pre-change tree; mutant (a) re-add `basinId` parse+serialize in `queryState.ts` → E2 red; mutant (b) re-add `param={{ name: 'basinId', queryKey: 'basinId' }}` on the `/basins/:basinId` route → E1 red. Record each red run, revert mutants |
| E7 | toolchain | `pnpm exec tsc --noEmit -p tsconfig.app.json && pnpm check:types && pnpm test && pnpm build` | all green |
| E8 | live display (node-27 C4 browser lane, `docs/runbooks/node-27-bringup-checklist.md`) | agent-browser on `https://test.nwm.ac.cn/basins/basins_qhh?source=ifs` | pre-change baseline captured 2026-09-13 (`.workplans/issue-2109/evidence/e8-baseline.txt`): lands on `/?source=ifs&validTime=…`, `全国总览地图`, no back button. Post-deploy recapture must match; deployment is an operator action after merge, so the post-deploy receipt is recorded as a follow-up if it is not yet deployed at merge time |

E4 command (run from the repo root):

```bash
git grep -nE "BasinDetailMode|useBasinDetailMode|loadBasinDetail|basinDetail\b|basinLoading|basinError|basin-river-segments|basinSegments|m11-back-to-overview|BackToOverview|fetchLineage|derivedTimeline|derivedTimes|basinDetailToOverviewBasin|BasinDataSnapshot|fetchRunsForBasinVersion|basinSnapshot(Metadata)?MatchesQuery|M11MapSurface\b|M11TimelineDerivedTimes" -- apps/frontend/src apps/frontend/e2e ':!*.md'
```

## Non-Goals (explicit)

- Backend routes, OpenAPI, `docs/spec/04_api_design.md` lineage section (#2039).
- National-mode bugs #2131 / #2103 / #2127 / #2139; file split #2102.
- Repairing pre-existing spec drift outside basin-detail clauses (design D5).
- Camera fit for legacy basin links.
