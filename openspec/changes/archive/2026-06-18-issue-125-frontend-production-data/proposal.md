## Why

Issue #125 tracks remaining frontend productionization gaps under Epic #120. The frontend currently builds and has useful mocked tests, but the first-screen forecast map still uses a hard-coded demo river network, flood alert requests bypass the shared OpenAPI client/API base, operator role switching is a frontend-only demo control, monitoring trend requests do not carry the selected source/scenario context, and the Vite build emits large chunk warnings.

These gaps can make production deployments look functional while querying the wrong backend, allowing UI-only role spoofing, or rendering forecast/flood views against inconsistent river segment identifiers.

## What Changes

- Replace the forecast page demo river network with backend river segment/network data so map clicks use real `basin_version_id`, `river_network_version_id`, and `segment_id` values.
- Route flood alert and tile requests through shared API-base-aware helpers so `VITE_API_BASE_URL` consistently affects forecast, flood alerts, monitoring, and tiles.
- Make the frontend RBAC boundary production-safe: remove demo role switching in production and derive action headers from a configured/auth-provided role source rather than a user-controlled dropdown.
- Align flood alert timeline/threshold normalization with OpenAPI-generated types and reduce local type drift.
- Add source/scenario filtering to monitoring trend requests and backend metrics as needed.
- Address Vite large chunk warnings with intentional chunking or a documented threshold.
- Add focused unit/E2E coverage for production API base, RBAC boundary, flood alerts page behavior, SPA fallback, and build/test commands.

## Capabilities

### New Capabilities

- `frontend-production-readiness`: Frontend pages use production-safe data sources, deployment API-base handling, RBAC boundaries, and monitoring filters.

## Impact

- Affects frontend map, flood alert, monitoring, auth/RBAC, API client, Vite config, and tests under `apps/frontend/`.
- May require small backend/OpenAPI changes for river network GeoJSON/listing and metrics source/scenario filters.
- Does not implement the real Postgres/PostGIS/TimescaleDB integration matrix from #126.
