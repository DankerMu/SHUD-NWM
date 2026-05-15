## Context

Issue #125 is part of Epic #120 and is unblocked by #123. #124 has merged, so production Slurm path work is out of scope except where frontend monitoring consumes those APIs. Current risk is that frontend behavior remains demo/mock-friendly while production deployments require consistent API base, real river segment identifiers, non-spoofable authorization headers, and deployable chunks.

Fixture level: expanded
Project profile: other

Change surface:
- Forecast map/data: `apps/frontend/src/components/map/MapView.tsx`, `RiverLayer.tsx`, `apps/frontend/src/stores/forecast.ts`
- Flood alerts: `apps/frontend/src/stores/floodAlert.ts`, `FloodAlertMap.tsx`, `FloodReturnPeriodLayer.tsx`, `FloodAlertPage.tsx`
- Monitoring: `apps/frontend/src/stores/monitoring.ts`, `TrendPanel.tsx`, `MonitoringPage.tsx`, `apps/api/routes/pipeline.py`
- RBAC/API client: `apps/frontend/src/api/client.ts`, `stores/auth.ts`, `AppShell.tsx`, `RBACGate.tsx`, `JobsTable.tsx`
- Deployment/build: `apps/frontend/vite.config.ts`, E2E and unit tests
- Potential backend/OpenAPI support: `apps/api/routes/models.py`, `apps/api/routes/forecast.py`, `openapi/nhms.v1.yaml`

Must preserve:
- Forecast series requests keep using `GET /api/v1/basin-versions/{basin_version_id}/river-segments/{segment_id}/forecast-series`.
- Flood alert summary/ranking/timeline UI behavior and warning-level filters remain compatible.
- Monitoring retry/cancel still sends role headers accepted by backend tests, but production UI must not let arbitrary users pick those roles locally.
- `corepack pnpm test` and `corepack pnpm build` stay green.
- Existing API envelopes and generated OpenAPI types remain the source of truth.
- Playwright mocked tests continue to work without a live backend.

Must add/change:
- Forecast river network loader must call backend data rather than returning `demoRivers`; loaded features must carry `segment_id`, `basin_version_id`, `river_network_version_id`, stream order/name where available, and valid GeoJSON LineString/MultiLineString geometry.
- Flood alert store/layers must use the shared API base. Direct `fetch('/api/...')` calls must be replaced or wrapped so `VITE_API_BASE_URL=https://api.example.test` sends requests to that base.
- Auth/RBAC must have an explicit production boundary. Production builds must not expose a role dropdown that can grant operator/model_admin/sys_admin locally. If demo switching remains, it must be gated by a dev/test flag and default production role source must be headers/config/auth context.
- Monitoring metrics endpoints and frontend calls must support source/scenario filters consistently. Jobs and trend charts should use the same selected source/scenario context where applicable.
- Flood alert local threshold/timeline types must not contradict OpenAPI-generated schemas; any local convenience type must be a narrow normalized view with tests proving API shape compatibility.
- Vite build chunk warning must be intentionally handled by manual chunks for MapLibre/ECharts/vendor or an explicit checked threshold.
- E2E must cover forecast real river data loading, flood alerts page/API-base behavior, production RBAC boundary, and SPA fallback. Unit/store tests must cover API-base and query parameters.

## Risk Packs Considered

- Public API / CLI / script entry: selected - frontend pages call public backend routes and operator actions send authorization headers.
- Config / project setup: selected - `VITE_API_BASE_URL`, Vite chunking, Playwright preview/server settings, and production/dev role flags change.
- File IO / path safety / overwrite: not selected - no local file writes or publish/delete behavior intended.
- Schema / columns / units / field names: selected - GeoJSON properties, OpenAPI-generated flood types, monitoring metric filters, and run/job fields are contracts.
- Geospatial / CRS / shapefile sidecars: selected - river segment geometries and map layers must consume backend CRS/GeoJSON correctly.
- Time series / forcing / temporal boundaries: selected - forecast series, flood timeline valid times, and monitoring trend windows are user-facing temporal contracts.
- Numerical stability / conservation / NaN: not selected - no numerical hydrology calculations change in frontend.
- Solver runtime / performance / threading: not selected - no SHUD runtime or worker execution change.
- Resource limits / large input / discovery: selected - large river GeoJSON, ECharts/MapLibre chunks, pagination, and bundle size are affected.
- Legacy compatibility / examples: selected - demo/test mocks must remain available without being production authority.
- Error handling / rollback / partial outputs: selected - API failures, empty river networks, denied actions, and SPA fallback must show deterministic UI.
- Release / packaging / dependency compatibility: selected - production build, API type generation, chunk config, and browser routing are deployment-sensitive.
- Documentation / migration notes: selected - production RBAC/API-base boundary needs a short documented or test-enforced contract.

## Required Evidence

- Forecast map test: mocked backend river network -> MapView renders features and clicked segment request contains backend `basin_version_id`/`segment_id`.
- API-base test: `VITE_API_BASE_URL` -> forecast/flood/monitoring/tile requests target the configured base.
- RBAC test: production mode -> no role dropdown grants operator privileges; retry/cancel headers use configured/auth role only.
- Monitoring test: selected source/scenario -> metrics endpoints receive matching query params and backend filters results.
- Flood alert type test: API timeline/threshold payload -> normalized frontend state matches generated OpenAPI fields without local drift.
- Build/deploy evidence: `corepack pnpm test`, `corepack pnpm build`, and targeted E2E/preview fallback pass.

## Non-Goals

- Real Postgres/PostGIS/TimescaleDB integration matrix from #126.
- Full authentication provider integration beyond removing/gating frontend-only privilege escalation and honoring configured role input.
- Redesign of backend authorization policy.
- New map styling beyond what is necessary for real river/flood data.

## Review Focus

- Ensure production API base is applied to every non-OpenAPI direct fetch, especially GeoJSON/tile fetches.
- Ensure role switching cannot grant production operator actions from the browser alone.
- Ensure real river feature identifiers line up with forecast-series paths and flood detail paths.
- Ensure source/scenario filtering is implemented on both frontend and backend/OpenAPI where needed.
- Ensure build chunk handling is intentional and does not hide real bundle regressions.
