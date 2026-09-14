## Why

The frontend basin-detail lane is unreachable by construction (#2109): the only
two `basinId` writers in the query state write `null`, a basin click on the
national map only moves the camera, and a `/?basinId=` or `/basins/:basinId`
deep link is stripped on first mount before `BasinDetailMode` can render. Its
store action `loadBasinDetail` has no production caller that ever runs.

The owner decided on 2026-09-13 to delete the lane instead of wiring it back
(#2109 option B, decision comment
https://github.com/DankerMu/SHUD-NWM/issues/2109#issuecomment-5657234086):

- The product already moved to "stay on the national map": a basin click fits
  the camera and never drills in, and #2014 shipped only the national half of
  the control bar.
- The national overview already delivers the lane's user value in place:
  river-segment click opens the GFS+IFS forecast panel, met-station click opens
  the forcing panel, and only the national mode has the bottom control bar and
  the precipitation overlay.
- Wiring it back would immediately surface #2110 (run-scoped valid times
  discarded, including a backend metadata-merge question), #2132, missing run
  cycle exposure, and the unbuilt basin half of #2014 — a cross-plane M–L cost
  for a view that is a regression of the national one.

Meanwhile the dead lane keeps consuming review and CI budget (PR #2101 spent two
semantic fix rounds on it) and 12 canonical specs still mandate a basin-detail
page that no user can reach, contradicting
`evidence-boundary-hardening` ("`/basins/:id` only as legacy redirect").

## What Changes

- **Specs (this change)**: remove or rewrite every canonical requirement that
  mandates basin-detail behaviour; state the retired contract once as a
  compatibility requirement (legacy `/basins/:basinId` and `?basinId=` links land
  on the national overview).
- **Frontend (implementation issue)**: delete `BasinDetailMode`,
  `useBasinDetailMode`, `loadBasinDetail` and the `basinDetail` / `basinLoading`
  / `basinError` store slices, the basin-scoped segment layer
  (`basinSegments` / `basin-river-segments`), the back-to-overview button, the
  first-mount `basinId` strip gate, the `basinId` query-state field, the
  `derivedTimeline` argument of the national valid-time correction, the
  helpers that become unreferenced, and their tests. Keep `/basins/:basinId` as a
  legacy redirect to `/` that drops the path parameter and preserves every other
  query key. Add a vitest pin for both legacy link forms.
- **Other active changes** (design D4): requirements that
  `display-v2-national-timeline-precip-overlay` also MODIFIES get their
  basin-detail clauses removed inside display-v2's own spec deltas, and this
  change leaves those headers alone; display-v2's `design.md` / `tasks.md` prose
  only gains supersession notes. The stale, currently un-archivable
  `m11-popup-station-overlay-usability` gets the same basin-detail edits in its
  deltas, and its archive defects are routed to #2326.

**BREAKING (spec-level only)**: the basin-detail page contract is retired. No
user-visible behaviour changes: every legacy link already lands on the national
overview today, and still does.

## Non-Goals

- No camera fit to the basin named by a legacy link (kept as today; YAGNI).
- No backend change: `/api/v1/basins/*`, `/api/v1/runs`, `/api/v1/layers?run_id=`,
  `/api/v1/lineage/river-point` and `hydro_display.py` metadata merge stay as is.
- No change to the national overview's behaviour, the `/segments/:segmentId`
  legacy redirect, `source=best|compare` parsing, or the popups.
- No split of `overviewData.ts` / `overviewDataContracts.ts` (#2102), no fix of
  #2131 / #2103 / #2127 / #2139 (national-mode issues, handled separately).

## Capabilities

Modified (canonical specs under `openspec/specs/`): see `specs/` — one delta per
affected capability.

## Impact

- Frontend: `apps/frontend/src/{App.tsx,pages/OverviewPage.tsx,components/m11/BasinDetailPanels.tsx,components/map/*,pages/m11/M11Controls.tsx,stores/overviewData.ts,lib/m11/{queryState.ts,overviewDataContracts.ts}}` and tests.
- Issues closed as superseded once the frontend PR merges: #2110, #2132. #2039
  loses its frontend half (`fetchLineage` lives only in `loadBasinDetail`).
- `design.md` exemption: none — expanded fixture (routing + legacy compatibility).
