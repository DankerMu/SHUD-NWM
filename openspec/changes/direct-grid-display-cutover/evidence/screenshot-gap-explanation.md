# Screenshot gap explanation — Epic #992 SUB-7 (Issue #999)

## Summary

The retention-empty-state page-composition screenshot from
`https://test.nwm.ac.cn/` was **NOT** captured showing the
`m11-station-popup-empty` retention message. Phase B failed on a
timing race + never-clicked-anything bug. Phase C fixed the timing race
(`SCREENSHOT_WINDOW_SECONDS=300`) and rewrote the Playwright spec to
drive a real UI click sequence, but the click could not be delivered
to the maplibre map inside the deployed frontend (see §2 below).

The Phase C screenshot **was** captured (`rehearse/retention-empty-state.png`,
384 KB, full-page) — but it shows the national overview map at flip
moment, not the retention popup. It is included as a live-app
receipt (page composition and layer switcher operating during the
committed cutover window), not as the retention-state proof.

This document records the residual gap and enumerates why the gap is
bounded (does not undermine the display-cutover certification).

## 1. What was attempted (Phase B → Phase C)

### Phase B (`eb58bb39`, first execution)
- `nwm-retention-empty-state.spec.ts` navigated to `/`, waited for
  `m11-fullscreen-map` to be visible, then raced `waitFor visible` on
  `m11-station-popup-empty` or `m11-station-popup-partial` with a 60 s
  timeout.
- The spec **never fired a click** — no station pin was clicked, no
  cycle picked, so the two testids never surfaced.
- `rehearse.py`'s `SCREENSHOT_WINDOW_SECONDS = 30` closed before the
  Playwright test could run out its own 60 s waitFor timeout — the
  restore transaction had committed by the time the test errored with
  `page.screenshot: Target page, context or browser has been closed`.

### Phase C (`af4a1427` and `814dff9f`, this retry)
- Extended `SCREENSHOT_WINDOW_SECONDS` to 300 (5 min).
- Rewrote the spec (300 s test timeout) to:
  1. Neutral mount at `/`, wait for map.
  2. `page.goto('/?basinId=basin__evidence_cmfd_p02_synth&metStations=1')`
     — the neutral mount ensures the first-mount `basinId` strip guard
     (`OverviewPage.tsx:74-84`) does not fire.
  3. `page.evaluate` to look up the maplibre-gl map instance on the
     `.maplibregl-canvas-container` DOM node (`_map` /
     `__reactMapGlMap` slots), call `map.project([100.0, 30.0])` to
     translate the synth station lng/lat into canvas pixel
     coordinates, then dispatch synthetic `mousedown/mouseup/click`
     events on the canvas at that pixel.
  4. On popup open, click the `m11-popup-issue-time` picker and
     select the last (oldest) option so both GFS + IFS series
     requests hit `STATION_FORCING_FILE_NOT_FOUND` → retention miss.
  5. Wait up to 180 s for `m11-station-popup-empty` or
     `m11-station-popup-partial`.
  6. Screenshot regardless (full-page) so the receipt captures the
     live state at flip moment.
- Also always attach a `rehearsal-observation-summary.json` to the
  Playwright trace so post-mortem is auditable when the popup does not
  open.

## 2. Why the Phase C UI-click sequence did not reach the popup

The Playwright trace `rehearsal-observation-summary.json` recorded:

```json
{
  "click_result": {"ok": false, "reason": "map-instance-not-found"},
  "popup_opened": false,
  "empty_state_visible": false,
  "partial_state_visible": false,
  "retention_text_observed": false
}
```

Root cause of `map-instance-not-found`:
- react-map-gl v7 does not expose the underlying maplibre-gl map on
  the container DOM node's `_map` or `__reactMapGlMap` slot in
  production bundles. The map instance is held in a React ref that is
  not reachable from `page.evaluate` without a debug hook (e.g. a
  `window.__NHMS_MAP_DEBUG__` handle wired conditionally under
  `VITE_ENABLE_DEBUG_HANDLES`). No such hook currently exists in the
  frontend.
- Even if the map instance were reachable, the synthetic basin
  (`basin__evidence_cmfd_p02_synth`) has **no** `basin_boundary`
  geometry in `core.basin_version` on node-27 (the archived readiness
  change registered the basin identity for evidence but did not
  provision a polygon). So `?basinId=...` navigation would render
  BasinDetailMode with a fallback CHINA_VIEW camera and no basin
  polygon — the synth stations at (100.0, 30.0), (100.5, 30.0),
  (100.0, 30.5) would be off-screen at the fallback zoom level.
- The synth station rows also cannot be discovered via the national
  overview met-stations MVT layer because the synth basin was NOT
  present in the overview's basin list at flip moment (the overview
  fetch has its own basin-selection semantics that require boundary
  geometry).

The Phase C spec's screenshot therefore captured the live national
overview at flip moment (see `rehearse/retention-empty-state.png`) —
which is a valid page-composition receipt of the frontend continuing
to render correctly during the committed cutover, but is NOT visual
proof of the `m11-station-popup-empty` retention message.

## 3. Why this residual gap is bounded

The retention empty-state code path is protected by two orthogonal
receipts that together cover the invariant this screenshot was meant
to prove:

### 3a. Unit-test regression lock (SUB-3 T1/T2/T3)

`apps/frontend/src/components/map/__tests__/M11StationForcingPopup.test.tsx`
lines 808-997 lock the exact retention flow at the code level:

- **T1 (line 808):** with BOTH GFS + IFS returning
  `STATION_FORCING_FILE_NOT_FOUND` for a pre-cutover cycle,
  `m11-station-popup-empty` renders with the retention message
  `已不在当前磁盘保留窗口内`, no chart is drawn, and no fallback
  endpoint is hit (exactly 2 series requests to
  `/api/v1/met/stations/{station_id}/series`).
- **T2 (line 886):** the picker offers only catalog-provided cycles;
  no synthesis path adds a pre-cutover option.
- **T3 (line 918):** the retention warning is per-session; no
  `localStorage` / `sessionStorage` write persists it across
  unmount/remount.

These three unit tests exercise exactly the DOM / rendering / network
contract the live screenshot would have visually confirmed. They run
as part of the frontend test suite on every push and are the
load-bearing regression lock for the retention path.

### 3b. Display-plane invariant (MVT source-identity SQL diff)

`rehearse/mvt-source-identity.before.txt` and
`rehearse/mvt-source-identity.after.txt` record:

```
BEFORE: met-stations:2bfc915b79ad9dbe:basin__evidence_cmfd_p02_synth__v1:3
AFTER:  met-stations:f03703b827fc1462:basin__evidence_cmfd_p02_synth__v1:3
```

The station cardinality is stable (3 before / 3 after) but the
station-id / role / grid_snapshot_id checksum flips from
`2bfc915b79ad9dbe` to `f03703b827fc1462`. This is the exact input the
station-MVT source-identity computer (`_station_source_version` at
`apps/api/routes/hydro_display.py:582-620`) hashes into the tile
version string, so the same query at the live display API responds
with a different version — the frontend's TileJSON cache
self-invalidates, and any freshly opened M1 cell-station pin on a
pre-cutover cycle after the flip will request a station-series file
that does not exist for the old cycle → 
`STATION_FORCING_FILE_NOT_FOUND` → retention empty state code path
(the same one exercised by SUB-3 T1).

### 3c. Downstream frontend live behavior

The Phase C screenshot itself proves that on the live
`https://test.nwm.ac.cn/` public host, during the committed cutover
window:
- The frontend rendered without error, populated all 13 production
  basins (`Heihe`, `Qhh`, `Zhaochen / *`, `Weiganhe`, `Kashigeer`,
  `Keliya`, `Hetianhe`, `Tailanhe`, `Qinyijiang`, `Xinanjiang Upstream`).
- The met-stations layer switcher and hydro-layer switcher were both
  operational.
- The status note `已加载 5000 个代站，列表已截断（总数未完全统计）`
  confirms met-stations were loading from the display API and being
  clustered — the live tile pipeline was functional at flip moment.

## 4. Zero-impact + certification status

- **Zero-impact anchor:** HOLDS at all 3 observation points
  (pre / during / post-restore = 6290 production stations across 13
  basin_version_ids). See `rehearse/production-scoped-assertions.*.log`.
- **Change 4 activate/deactivate + hooks:** exercised end-to-end,
  `rehearse.py` returned `rc=0`. See `rehearse/rehearse.node-27.pass.log`.
- **Change 5 state-clone hook (approved-skip):** engaged inside the
  activation tx, `ops.audit_log` row recorded same-tx.
- **Station-flag flip hook:** 3 M1 mirror rows flipped active_flag=true
  in same tx; legacy 3 synth-station rows flipped false. Restore
  reverted both.
- **Scheduler plane:** 0 new `hydro.hydro_run` rows for any
  `model__evidence%` model during the window; 0 evidence models in
  the post-restore active set.
- **Retention empty-state screenshot:** BOUNDED GAP recorded in this
  file. Certification is unaffected: the SUB-3 unit tests lock the
  retention code path; the MVT source-identity SQL diff proves the
  display-plane input flip; the Phase C page-composition screenshot
  proves the live frontend operated correctly at flip moment.

## 5. Decision handoff to SUB-8 (Epic #992 close)

**Recommendation for Epic #992 SUB-8:** ACCEPT the bounded gap and
proceed to Epic close.

Rationale:
- The retention path is regression-locked at the code level (SUB-3
  T1/T2/T3) and at the display API contract level (MVT source-identity).
- The screenshot's marginal contribution — visually confirming the DOM
  composition of one popup empty-state — is genuinely covered by T1,
  which mocks the exact HTTP response the retention miss produces and
  asserts the exact DOM testid/text.
- Adding the map debug hook to make Playwright deterministic on
  maplibre features is a scope creep (frontend production build now
  needs a conditional debug handle just to satisfy one rehearsal
  screenshot). This is disproportionate to the bounded risk.

If the gap must be closed later:
- Add a conditional `window.__NHMS_MAP_DEBUG__` handle in
  `apps/frontend/src/components/map/M11MapLibreSurface.tsx` gated on
  `import.meta.env.VITE_ENABLE_DEBUG_HANDLES`.
- Provision the synth basin polygon geometry alongside the M0/M1
  registration so the map can fit + click the synth stations.
- Alternatively, target the retention screenshot at a real production
  basin whose scheduler is stopped for the rehearsal (out of scope
  for evidence-only rehearsal — that would be a real cutover
  rehearsal, not the synthetic-identity one).

## 6. Companion evidence

- `rehearse/retention-empty-state.png` — Phase C page-composition
  screenshot (national overview at flip moment).
- `rehearse/rehearse.node-27.pass.log` — full Phase C activation +
  hooks + restore trace (`rc=0`).
- `rehearse/mvt-source-identity.{before,after}.txt` — MVT source
  identity flip that self-invalidates the frontend cache.
- `apps/frontend/src/components/map/__tests__/M11StationForcingPopup.test.tsx`
  lines 808-997 — SUB-3 T1/T2/T3 unit tests locking the retention
  empty-state contract at the code level.
