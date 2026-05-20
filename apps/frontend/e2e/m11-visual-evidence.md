# M11 Visual Evidence Notes

Use the browser automation tool outside unit tests after starting the frontend dev server.
Issue #165 screenshot evidence was captured on 2026-05-19 Asia/Shanghai and stored in local
issue-scoped paths under `.codex/screenshots/issue-165/`. Those PNG files are local review
evidence and are not committed.
M11 evidence predates the M15 deterministic Playwright capture lane. For any refreshed PR evidence,
prefer `apps/frontend/e2e/m15-visual-conformance.spec.ts`, which blocks unexpected non-local
network traffic and fulfills known external map tile/style hosts with deterministic stubs.

```bash
cd apps/frontend
corepack pnpm dev --host 127.0.0.1 --port 5175
```

Required global-argument command form:

```bash
agent-browser --session issue-165-parent --args "--no-sandbox,--disable-dev-shm-usage,--disable-gpu" open <url>
agent-browser --session issue-165-parent wait --load networkidle
agent-browser --session issue-165-parent set viewport <width> <height>
agent-browser --session issue-165-parent screenshot <output-path>
```

Successful issue #165 evidence matrix:

| Route | Viewport | Output path | Captured | Inspection status |
| --- | --- | --- | --- | --- |
| `/overview` | 1920x1080 | `.codex/screenshots/issue-165/m11-overview-1920x1080.png` | yes | inspected for navigation, panels, map area, controls, and timeline overlap |
| `/overview` | 1440x900 | `.codex/screenshots/issue-165/m11-overview-1440x900.png` | yes | inspected for navigation, panels, map area, controls, and timeline overlap |
| `/overview` | 1280x900 | `.codex/screenshots/issue-165/m11-overview-1280x900.png` | yes | inspected for compact-panel fit, map area, and timeline overlap |
| `/basins/basin-demo?source=gfs&basinVersionId=bv-001&segmentId=seg-009` | 1920x1080 | `.codex/screenshots/issue-165/m11-basin-1920x1080.png` | yes | inspected for basin route panels, river drilldown map area, selected segment panel, and timeline overlap |
| `/basins/basin-demo?source=gfs&basinVersionId=bv-001&segmentId=seg-009` | 1440x900 | `.codex/screenshots/issue-165/m11-basin-1440x900.png` | yes | inspected for basin route panels, river drilldown map area, selected segment panel, and timeline overlap |
| `/basins/basin-demo?source=gfs&basinVersionId=bv-001&segmentId=seg-009` | 1280x900 | `.codex/screenshots/issue-165/m11-basin-1280x900.png` | yes | inspected for compact-panel fit, selected segment panel, and timeline overlap |

Traceability:
- Command transcript and early CDP timeout attempts are recorded in
  `.codex/visual-evidence/issue-165/browser-attempts.md`.
- A parent retry captured the matrix above and copied the generated PNGs into
  `.codex/screenshots/issue-165/`.
- The required overview and basin-detail viewports at 1920x1080, 1440x900, and 1280x900 are all
  present locally.

## Issue #165 developer notes

Routes:
- `/` and `/overview` render the national overview workflow.
- `/basins/:basinId` renders the basin drill-down workflow and restores `source`, `cycle`,
  `validTime`, `layer`, `basemap`, `basinVersionId`, `segmentId`, `warningLevel`, and `q`.
- `/forecast`, `/flood-alerts`, and `/monitoring` remain the implemented handoff
  destinations.

Data contracts:
- Overview and basin pages consume typed view models from
  `src/lib/m11/overviewDataContracts.ts` and `src/stores/overviewData.ts`.
- Basin maps use normalized `BasinSegmentRow.geometry` only when it passes the selected
  segment geometry budget and the aggregate basin-river collection budget. Missing, malformed,
  oversized, or over-budget river geometry is omitted with a scoped unavailable note; no river
  coordinates are fabricated.
- Selected segment detail uses `SelectedSegmentDetail` for IDs, basin/model metadata, Q,
  return period, warning state, forecast time, source/cycle provenance, quality, lineage,
  trend points, comparison availability, and forecast handoff URL.

Source/scenario behavior:
- `gfs` and `ifs` resolve to concrete forecast APIs.
- `best` must expose the concrete resolved source when available; handoff URLs use that
  concrete source instead of a backend-only best-available value.
- `compare` keeps unavailable flood ranking/timeline/lineage states scoped until a real
  GFS+IFS aggregation endpoint exists.

Map layer limits:
- Basin detail renders river segments from the bounded river-segment GeoJSON returned by
  existing basin-version APIs. It colors the same river network by discharge, return period,
  or warning level depending on the active layer.
- Basin detail passes real basin boundary context from the loaded basin version when available.
  City and station labels remain explicitly unavailable because M11 currently has no
  city/station label contract or feed.
- Production MVT/PBF river-network tiles, meteorology grids, DEM, station layers, and city or
  station label feeds are future contracts. Their controls remain unavailable or placeholder
  backed and must not claim rendered data.

Handoff destinations:
- `查看详情` currently hands off to the existing `/forecast` route with basin/version/segment
  context.
- `对比预报` renders in-panel labeled GFS/IFS comparison values when comparable series exist;
  when unavailable it stays disabled with a scoped reason. Full-screen segment detail, model
  asset management, meteorology pages, and production MVT remain deferred follow-ups.
