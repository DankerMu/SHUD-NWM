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
