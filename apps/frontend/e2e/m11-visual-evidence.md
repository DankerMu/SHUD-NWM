# M11 Visual Evidence Notes

Use the browser automation tool outside unit tests after starting the frontend dev server.
Issue #160 captured the screenshots below with `agent-browser` on 2026-05-18.

```bash
cd apps/frontend
corepack pnpm dev --host 127.0.0.1 --port 5174
```

Required visual evidence matrix:

| Route | Viewport | Command | Output path | Attempted | Passed |
| --- | --- | --- | --- | --- | --- |
| `/overview` | 1920x1080 | `agent-browser --session m11-issue-160 --args "--no-sandbox,--disable-dev-shm-usage,--disable-gpu" open "http://127.0.0.1:5174/overview"`; `agent-browser --session m11-issue-160 set viewport 1920 1080`; `agent-browser --session m11-issue-160 screenshot apps/frontend/artifacts/m11-overview-1920x1080.png` | `apps/frontend/artifacts/m11-overview-1920x1080.png` | yes | yes |
| `/overview` | 1440x900 | `agent-browser --session m11-issue-160 set viewport 1440 900`; `agent-browser --session m11-issue-160 screenshot apps/frontend/artifacts/m11-overview-1440x900.png` | `apps/frontend/artifacts/m11-overview-1440x900.png` | yes | yes |
| `/overview` | 1280x900 | `agent-browser --session m11-issue-160 set viewport 1280 900`; `agent-browser --session m11-issue-160 screenshot apps/frontend/artifacts/m11-overview-1280x900.png` | `apps/frontend/artifacts/m11-overview-1280x900.png` | yes | yes |
| `/basins/basin-demo?source=best&basinVersionId=bv-demo&segmentId=seg-demo` | 1920x1080 | `agent-browser --session m11-issue-160 open "http://127.0.0.1:5174/basins/basin-demo?source=best&basinVersionId=bv-demo&segmentId=seg-demo"`; `agent-browser --session m11-issue-160 set viewport 1920 1080`; `agent-browser --session m11-issue-160 screenshot apps/frontend/artifacts/m11-basin-1920x1080.png` | `apps/frontend/artifacts/m11-basin-1920x1080.png` | yes | yes |
| `/basins/basin-demo?source=best&basinVersionId=bv-demo&segmentId=seg-demo` | 1440x900 | `agent-browser --session m11-issue-160 set viewport 1440 900`; `agent-browser --session m11-issue-160 screenshot apps/frontend/artifacts/m11-basin-1440x900.png` | `apps/frontend/artifacts/m11-basin-1440x900.png` | yes | yes |
| `/basins/basin-demo?source=best&basinVersionId=bv-demo&segmentId=seg-demo` | 1280x900 | `agent-browser --session m11-issue-160 set viewport 1280 900`; `agent-browser --session m11-issue-160 screenshot apps/frontend/artifacts/m11-basin-1280x900.png` | `apps/frontend/artifacts/m11-basin-1280x900.png` | yes | yes |

Representative 1280x900 screenshots were visually inspected for navigation, side panels,
map placeholder area, and timeline overlap before the PR review rerun.

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
  segment geometry budget. Missing, malformed, or oversized river geometry is omitted with a
  scoped unavailable note; no river coordinates are fabricated.
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
- Production MVT/PBF river-network tiles, meteorology grids, DEM, station layers, and city or
  station label feeds are future contracts. Their controls remain unavailable or placeholder
  backed and must not claim rendered data.

Handoff destinations:
- `查看详情` currently hands off to the existing `/forecast` route with basin/version/segment
  context.
- `对比预报` is an in-panel availability/overlay toggle when comparable series exist; when
  unavailable it stays disabled with a scoped reason. Full-screen segment detail, model asset
  management, meteorology pages, and production MVT remain deferred follow-ups.

Issue #165 local visual evidence target:
- `.codex/screenshots/issue-165/` for screenshots.
- `.codex/visual-evidence/issue-165/` for command transcripts or limitation notes.
