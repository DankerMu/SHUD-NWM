# M11 Visual Evidence Notes

Use the browser automation tool outside unit tests after starting the frontend dev server.
Do not commit screenshots unless the parent workflow requests artifacts.

```bash
cd apps/frontend
corepack pnpm dev --host 127.0.0.1 --port 5174
agent-browser --args "--no-sandbox,--disable-dev-shm-usage,--disable-gpu" open http://127.0.0.1:5174/overview
agent-browser screenshot artifacts/m11-overview-1440x900.png
agent-browser --args "--no-sandbox,--disable-dev-shm-usage,--disable-gpu" open "http://127.0.0.1:5174/basins/basin-demo?source=best&basinVersionId=bv-demo&segmentId=seg-demo"
agent-browser screenshot artifacts/m11-basin-1440x900.png
```

Repeat with 1920x1080, 1440x900, and 1280x900 browser viewports for `/overview` and `/basins/:basinId`.

