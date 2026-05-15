## 1. Forecast Real River Data

- [x] 1.1 Replace `demoRivers` as the production forecast map source with backend river network/segment GeoJSON data.
- [x] 1.2 Add or reuse backend/OpenAPI route(s) that return river segments for an active/default basin version with stable `segment_id`, `basin_version_id`, `river_network_version_id`, `name`, `stream_order`, and GeoJSON geometry.
- [x] 1.3 Add tests proving a clicked map segment requests forecast series with backend identifiers, not demo IDs.
- [x] 1.4 Preserve mocked/demo fixtures only for tests or explicit dev fallback, not production default behavior.

## 2. Shared API Base and Deployment

- [x] 2.1 Replace direct flood alert/tile `fetch('/api/...')` calls with shared API-base-aware helpers or OpenAPI client calls.
- [x] 2.2 Add tests for `VITE_API_BASE_URL` showing forecast, flood alerts, monitoring, and tiles use the same configured API base.
- [x] 2.3 Add SPA fallback/preview deployment coverage so deep links such as `/flood-alerts` and `/monitoring` load under production preview.
- [x] 2.4 Handle Vite large chunk warnings with explicit manual chunks or a checked threshold, and document the decision in tests/config.

## 3. RBAC Production Boundary

- [x] 3.1 Remove or gate the AppShell role selector so production users cannot elevate roles via a frontend dropdown.
- [x] 3.2 Define the role source used for operator action headers in production, with dev/test override only when explicitly enabled.
- [x] 3.3 Update monitoring retry/cancel tests to prove viewer cannot trigger actions and production mode has no privilege-granting selector.

## 4. Flood Alerts Type/API Alignment

- [x] 4.1 Align flood timeline and threshold normalization with OpenAPI-generated schemas; local types may be normalized views only.
- [x] 4.2 Ensure flood alert summary/ranking/timeline calls preserve run, valid_time, threshold, basin, and segment query behavior through the shared API client/base.
- [x] 4.3 Add flood alerts page E2E or equivalent component/store coverage for summary, ranking, map segment selection, detail timeline, and configured API base.

## 5. Monitoring Source/Scenario Filters

- [x] 5.1 Add source and scenario query support to stage-duration and success-rate metrics endpoints and OpenAPI when absent.
- [x] 5.2 Pass selected source/scenario filters from monitoring UI/store into jobs and trend requests consistently.
- [x] 5.3 Add backend and frontend tests showing trends filter by source/scenario and do not mix unrelated cycles.

## 6. Required Evidence

- [x] 6.1 `openspec validate issue-125-frontend-production-data --strict --no-interactive` passes.
- [x] 6.2 `cd apps/frontend && corepack pnpm test` passes.
- [x] 6.3 `cd apps/frontend && corepack pnpm build` passes without unexplained large chunk warnings.
- [x] 6.4 Targeted frontend E2E for forecast map, flood alerts, RBAC production boundary, API base, and SPA fallback passes or is explicitly justified if browser dependencies are unavailable.
- [x] 6.5 Backend/API tests for any changed routes pass, at minimum `uv run pytest -q tests/test_api.py tests/test_monitoring_api.py tests/test_flood_alerts_api.py`.
- [x] 6.6 `uv run ruff check .` passes if backend files change.

## Risk Pack Evidence Mapping

- Public API / CLI / script entry: tasks 1.2, 2.1, 3.2, 5.1, evidence 6.4/6.5.
- Config / project setup: tasks 2.2, 2.3, 2.4, 3.1, evidence 6.2/6.3.
- Schema / columns / units / field names: tasks 1.2, 1.3, 4.1, 5.1.
- Geospatial / CRS / shapefile sidecars: tasks 1.1, 1.2, 1.3.
- Time series / forcing / temporal boundaries: tasks 4.2, 5.1, 5.3.
- Resource limits / large input / discovery: tasks 2.4, 1.1.
- Legacy compatibility / examples: task 1.4 and test mock preservation.
- Error handling / rollback / partial outputs: tasks 2.3, 3.3, 4.3.
- Release / packaging / dependency compatibility: tasks 2.3, 2.4, evidence 6.2/6.3.
- Documentation / migration notes: tasks 2.4, 3.2.

## Non-Goals

- Real DB/e2e integration matrix from #126.
- Full auth provider implementation beyond a safe frontend boundary and configured role source.
- Changes to Slurm/orchestrator production templates from #124.
