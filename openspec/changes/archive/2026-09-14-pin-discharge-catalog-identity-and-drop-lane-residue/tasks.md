## Risk Triage

```text
Issue type: bug (#2140) + dead-code deletion (#2332)
Project profile: NHMS (openspec/project-profile.md)
Blast radius: medium
Fixture level: expanded
Repair intensity: medium
Upstream suggested level: absent (issue-scribe issues, both size S)
Why:
- #2140: request-generation identity of (source, cycle) keys across two catalog snapshots — ordering/state invariant
- #2332: shared contract type (`LayerState.validTimeSource`, `normalizeLayerStates` input) and map surface props shrink; request signature of `fetchRuns*`
- kept at medium: browser-only, no backend/API/file IO/auth change; every failure mode assertable in vitest
Selected risk packs:
- Concurrency / shared state / ordering
- Schema / columns / units / field names (LayerState / normalizeLayerStates shape)
- Hydro-met time series / forcing windows (domain: triple identity)
- Documentation / migration notes (docs drift)
OpenSpec change: pin-discharge-catalog-identity-and-drop-lane-residue (generated)
Evidence floor:
- openspec validate pin-discharge-catalog-identity-and-drop-lane-residue --strict --no-interactive
- openspec validate display-v2-national-timeline-precip-overlay --strict --no-interactive
- cd apps/frontend && pnpm exec tsc --noEmit -p tsconfig.app.json && pnpm check:types && pnpm test && pnpm build
- uv run ruff check tests/test_hydro_display_mvt_scaling.py
```

## Risk Pack Selection

| Pack | Selected | Reason |
|---|---|---|
| Public API / CLI / script entry | not selected | No route, URL key or backend change; `/api/v1/runs` request proven unchanged (E5). |
| Config / project setup | not selected | None. |
| File IO / path safety / overwrite | not selected | Browser code; one docstring. |
| Schema / columns / units / field names | selected | `validTimeSource` union narrows; `normalizeLayerStates` / `m11SelectedLayerUnavailableReason` / map runtime props lose inputs. |
| Auth / permissions / secrets | not selected | None. |
| Concurrency / shared state / ordering | selected | Writer/reader key identity inside one generation. |
| Resource limits / large input / discovery | not selected | No new requests; none removed. |
| Legacy compatibility / examples | not selected | Deleted exports have no production importer (E4 grep); national behaviour unchanged. |
| Error handling / rollback / partial outputs | not selected | Bootstrap-failure path explicitly unchanged (E3). |
| Release / packaging / dependency compatibility | not selected | None. |
| Documentation / migration notes | selected | Docstring and runbook drift. |
| Published NHMS artifacts / display identity (domain) | not selected | Tile template identical across catalogs by contract. |
| Hydro-met time series / forcing windows (domain) | selected | Active cycle / valid-times identity (#2140). |

## Must preserve

- Phase-3 request set and ordering (cycles, per-cycle valid times only for non-default pairs, precip index) for the no-flip case; zero valid-times requests on the default load.
- MapLibre discharge source identity: tile URL template and `maplibre_source_layer` from bootstrap to final snapshot.
- Bootstrap-failure behaviour (#2139 notice, skipped phase 3, scoped catalog used).
- #2334 re-derive behaviour and its tests.
- `mergeLayerCatalogs` / `mergeLayerStates` contracts and tests (time-less runless entries kept).
- `/api/v1/runs` request path, query object and cache key; the existing `allowedRunQueryKeys` assertion (still listing `'basin_id'`) is not edited.
- Fail-closed (time-less) runless discharge retention in `mergeLayerCatalogs` and the #2014 A2 exception in `mergeLayerStates`.
- Non-default source (IFS) pair resolution from the catalog's `default_source` plus `cyclesBySource`; the existing IFS tests in `overviewDataSourceSelection.test.ts` stay unmodified and green.
- Every existing assertion except the test cases that only pin deleted symbols (listed in the PR body, one line each).

## Seams under test

- `useOverviewDataStore.getState().loadOverview` with the `mockApi` harness (`src/stores/__tests__/overviewData*.test.ts`), with the runless and run-scoped `/api/v1/layers` responses distinguished by `run_id`.
- `OverviewPage` with the precip harness (`src/pages/__tests__/OverviewPagePrecipOverlay.test.tsx`) for the overlay-facing scenario.
- grep oracle (E4).

## Tasks

### 1. Fixture

- [x] 1.1 Proposal, design, tasks, ADDED delta for `overview-data-contracts`.
- [x] 1.2 Fixture review (iteration 1 revise: 8 gaps — runless-without-discharge would drop the layer, M11Controls comment hit by E4, E1/E2 run-scoped-key precondition and red source, E3 failure method, page-level overlay row, contract citation, E5 wording, two Must preserve rows — all applied; iteration 2 pass with one note — E1b mock alignment — applied); `openspec validate` strict for this change and display-v2.

### 2. Catalog identity (#2140)

- [x] 2.1 `stores/overviewData.ts` phase 2: when `bootstrapForSnapshot` is non-null **and its `layers` contain a `discharge` entry**, remove `discharge` from the scoped catalog before `mergeLayerCatalogs`; comment names the invariant (one discharge identity per generation, #2140).
- [x] 2.2 Tests E1, E1b, E2, E3, E8.

### 3. Lane residue (#2332)

- [x] 3.1 Delete the #2332 symbol list from `components/map/{m11MapPrimitives.tsx,M11MapLibreSurface.tsx,m11MapBuilders.ts,m11MapRuntime.tsx}` and `lib/m11/overviewDataContracts.ts`, including the `m11-basin-river-unavailable` branch and `'derived'` / `derivedValidTimes`; the comment in `pages/m11/M11Controls.tsx` naming `derivedValidTimes` is edited so E4 can be empty.
- [x] 3.2 Drop `basinId` from `fetchRuns` / `fetchRunsPage` / `fetchRunsPageByStatus`.
- [x] 3.3 Delete only the pinning tests: `components/map/__tests__/M11RiverTooltip.test.tsx`; `pages/__tests__/M11Shell.test.tsx` "builds basin river feature properties…" case and `basinSegment` fixture; `lib/__tests__/m11OverviewDataContracts.test.ts` derivedValidTimes case(s); `basinRiverUnavailableReason` prop in `components/map/__tests__/m11MapRuntime.test.tsx`. Any other test edit is a reported deviation.
- [x] 3.4 Docs drift: `tests/test_hydro_display_mvt_scaling.py` docstring (~2736), `overviewData.ts` phase-1 comment naming `fetchLayerValidTimes`, `docs/runbooks/current-production-ops.md` `stores/overviewData.ts:537` → symbol reference.
- [x] 3.5 Tests E4–E5.

### 4. Close-out

- [x] 4.1 PR closes #2140 and #2332 (PR #2338 merged as accb65819).
- [x] 4.2 After merge: archive this change.

## Evidence Mapping

| ID | Seam | Input | Expected |
|---|---|---|---|
| E1 | store `mockApi` (default `runs` = published `run`, so phase 2 uses the run-scoped key); `/api/v1/layers` override branches on `options.params?.query?.run_id`: undefined → discharge `default_cycle` C1 + list L1, `'run-001'` (the fixture run) → C2 + L2 (C1 ≠ C2, lists disjoint); precip index available | `loadOverview` default query (no cycle), settled | precondition: exactly one `/api/v1/layers` call without `run_id` and one with the fixture run id; discharge `activeNationalCycle === C1` and `validTimes` equals L1 normalized (red pre-change: C2 / L2); precip index requested for C1 only; no valid-times request |
| E1b | `OverviewPage` precip harness, both catalogs carry the `precip` entry, same flip; L1[0] must be in C1's precip index `valid_times` (e.g. C1 = `DEFAULT_CYCLE` with the fixture index) or the resolver stops at `window_incomplete` | render default URL, settled | `data-precip-hidden-reason` is not `index_pending`, the overlay URL carries C1 (red pre-change: `index_pending`) |
| E2 | same mocks as E1 | query with `cycle=C1` | same precondition; discharge `available`, `disabledReason` null (red pre-change: `pendingActiveCycleValidTimesDisabledReason`), `validTimes` = L1, no valid-times request |
| E3 | E1 mocks plus `/api/v1/basins` throwing (existing bootstrap-failure pattern in `overviewData.test.ts`) | default query | a run-scoped `/api/v1/layers` call happened; `bootstrapError` set; discharge from the scoped catalog (C2/L2). Green before and after (guard, not a red proof) |
| E8 | E1 mocks but runless `/api/v1/layers` returns a catalog without `discharge` | default query | discharge layer comes from the scoped catalog (available, C2/L2) — no disappearance. Green before and after (guard) |
| E4 | grep oracle (repo root) | `git grep -nE "M11BasinRiverPrimitive|M11SelectedSegmentPrimitive|M11_BASIN_RIVER_|M11_SELECTED_SEGMENT_|M11RiverTooltip|basinRiverUnavailableReason|m11BasinRiverCollectionBudget|buildBasinRiverFeatureCollection|m11BasinRiverLayerColor|hasBasinRiverNetwork|BasinSegmentRow|derivedValidTimes|numberOrZero|BasinRiverFeature|m11-basin-river-unavailable|'derived'" -- apps/frontend/src apps/frontend/e2e ':!*.md'` | 89 lines on `origin/master` f02dc2ebf; no output after |
| E5 | store `mockApi` request log | default overview load | `/api/v1/runs` calls: path and `params.query` equal (`toEqual`, undefined-insensitive — pre-change carried `basin_id: undefined`, which openapi-fetch's query serializer omits) to the expectation written in the test per ready status (`source`, `cycle_time`, `status`, `limit`, `offset` as today); the test must pass on both the pre-change and post-change store (run once on pre-change, record). Cache key unchanged by construction: `cacheKey` is `JSON.stringify`, which drops the undefined `basinId` |
| E6 | red proof | pre-change tree | E1 red on `activeNationalCycle`/`validTimes`, E1b red on `index_pending`, E2 red on the pending reason; E3 and E8 green; E5 green; record trimmed output under `.workplans/issue-2140/evidence/`. E4 prints 89 lines pre-change |
| E7 | toolchain | frontend full suite + ruff on the edited pytest file | `pnpm exec tsc --noEmit -p tsconfig.app.json && pnpm check:types && pnpm test && pnpm build` green; `uv run ruff check tests/test_hydro_display_mvt_scaling.py` clean; test count arithmetic in PR body (baseline 974 on `origin/master` f02dc2ebf) |
