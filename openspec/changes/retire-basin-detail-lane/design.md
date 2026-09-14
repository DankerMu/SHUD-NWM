## Context

`OverviewPage` renders `OverviewMode` or `BasinDetailMode` depending on
`state.basinId`. No path ever reaches the second branch (#2109): the first-mount
strip gate removes `basinId` from deep links, the redirect `/basins/:basinId` →
`/?basinId=` lands on that gate, and the only runtime `basinId` writer
(`backToOverview`) writes `null`. The owner decided to delete the lane
(decision B, 2026-09-13). The references below use symbol names, not line numbers.

## Goals / Non-Goals

Goals: remove the lane and every spec clause that mandates it, with zero
user-visible change and a pin that keeps legacy links landing on `/`.

Non-goals: see `proposal.md` (no backend change, no national-mode behaviour
change, no #2102 split, no national bug fixes).

## Decisions

### D1: Keep the legacy route, drop the key

`/basins/:basinId` stays a `LegacyRedirect` to `/` but no longer maps the path
parameter to a query key. `basinId` is removed from `M11QueryState`; a stray
`?basinId=` is then an unknown key that the existing
`needsM11QueryReplacement` → `replace` normalisation already strips. The
first-mount strip gate (`initialBasinStripRef`) becomes dead and is deleted.
Observable behaviour equals today's: every legacy link lands on the national
overview. Rejected: 404 the route (breaks bookmarks); camera-fit to the named
basin (new behaviour nobody asked for, YAGNI).

### D2: Delete by reachability, not by file

The deletion set is everything reachable only from `BasinDetailMode`:
`useBasinDetailMode`, `loadBasinDetail` and the `basinDetail` / `basinLoading` /
`basinError` slices, the store helpers and contract types that lose their last
caller (`fetchLineage`, `fetchRiverSegment(s)`, `fetchRunsForBasinVersion`,
`basinSnapshot*MatchesQuery`, `BasinDataSnapshot`, … — the implementer confirms
each by grep, because `tsconfig` has no `noUnusedLocals`), the basin-scoped
segment layer props (`basinSegments`, `selectedSegmentGeometry`,
`basin-river-segments` interaction id) on `M11MapLibreSurface`,
`M11BackToOverviewButton`, and the caller-less `M11MapSurface` wrapper in
`M11Controls.tsx` that forwards those props. The authoritative symbol list and
the grep oracle live in `tasks.md` (2.5–2.7, E4). Helpers still used by `OverviewMode` stay
(`bboxToMapFit`, `mapFeatureStringProperty`, `popupAnchorFromInteraction` move
out of `BasinDetailPanels.tsx` if that file would otherwise only hold them).

### D3: Remove the `derivedTimes` arm

`resolveM11NationalValidTimeCorrection` / `resolveM11ValidTimeCorrection`,
`buildM11TimelineViewModel` and `M11Timeline` accept payload-derived valid times
whose only producer is `useBasinDetailMode`. With every remaining caller passing
nothing, the arm is removed (`M11TimelineDerivedTimes`, the positional
parameter, the `'derived'` source label); the layer-derived path is unchanged
byte for byte.

### D6: Route pin seam

`App` owns its `BrowserRouter`, so the route table is extracted into an exported
`AppRoutes` rendered by `App` unchanged. The legacy-route pin renders
`AppRoutes` in a `MemoryRouter` with `OverviewPage` stubbed to print the
location, which pins the real route declaration without the page's API
harness; the national DOM markers are pinned separately on the real
`OverviewPage` (E2).

### D4: Archive-order safety with the other active changes

Two other active changes touch the same capabilities. When two changes MODIFY
the same requirement header, whichever archives second silently overwrites the
first one's text.

- `display-v2-national-timeline-precip-overlay` (active, archivable) MODIFIES
  four requirements that carry basin-detail text:
  `map-layer-timeline-controls` source controls and timeline (including the
  "Floating controls clear the control bar" back-button wording),
  `frontend-visual-conformance` UI tokens, and `map-first-layout-conformance`.
  It also carries two ADDED requirements with basin-detail clauses:
  `frontend-mvt-layer-consumption` (basin-detail scenario) and
  `precipitation-raster-overlay` (`best`/`compare` wording).
  This change does not touch any of those headers. Their basin-detail clauses
  are removed inside display-v2's own deltas, and display-v2's `design.md` /
  `tasks.md` prose only gains supersession notes (shared-change discipline).
  Tasks 6.5 and 6.6, left open only for the basin half, can then close.
  On a scratch copy, archiving the two changes in either order leaves every
  spec strict-valid with no basin-detail mandate left.
- `m11-popup-station-overlay-usability` (21/21 tasks done, last touched
  2026-07) MODIFIES `frontend-navigation-state` "URL query restores shareable
  state", `single-map-shell-routing` "旧展示路由收敛/重定向到单页" and
  `met-station-cluster-layer` under a renamed header. It **cannot be archived
  today, independent of this change**:
  - `map-feature-popups` MODIFIES the header "点击河段要素弹出 q_down
    预报曲线 + 重现期三态", which does not exist in canonical.
  - The `met-station-cluster-layer` header is renamed without a RENAMED block.
  - It asserts `/flood-alerts` and `layer=flood-return-period`, which exist in
    neither `App.tsx` nor `M11Layer`.

  Because of that, this change keeps ownership of those three headers, written
  against today's canonical text, so the canonical specs become basin-free as
  soon as this change archives. The same basin-detail edits are applied inside
  m11-popup's deltas so it does not reintroduce them. Repairing m11-popup (and
  rebasing its deltas onto the post-archive canonical text before archiving it)
  is routed to follow-up issue #2326, not done here.

### D5: Pre-existing spec drift is not repaired here

Several touched specs already describe panels, popups or routes the single-map
shell removed earlier (`national-overview-page` left/right panels and basin
popup rows, `segment-detail-route-state` full-screen route,
`met-station-cluster-layer` single selected basin). This change edits only the
basin-detail clauses; the rest is reported as follow-up, not fixed.

## Risks / Trade-offs

- Removing `basinId` from the query-state type can break a sibling consumer
  that spreads `M11QueryState` → caught by `tsc`; pinned by the route test.
- Deleting shared-surface props could drop a national render path → the
  national `OverviewMode` props are unchanged; `M11MapLibreSurface` tests and the
  national map tests must stay green without edits to their assertions.
- Rebuilding basin drill-down later starts from scratch; accepted by the owner.
