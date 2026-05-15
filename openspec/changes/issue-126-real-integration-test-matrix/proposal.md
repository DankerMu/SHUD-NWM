## Why

Issue #126 closes the last production-remediation gap under Epic #120: the project has broad SQLite/mock coverage, but the highest-risk paths still need repeatable verification against real PostgreSQL, PostGIS, TimescaleDB, Slurm command shims, and worker composition. Without this matrix, migrations, spatial SQL, production API queries, and orchestration smoke paths can drift while fast tests remain green.

## What Changes

- Add a real integration test matrix that runs migrations from zero on PostgreSQL with PostGIS and TimescaleDB extensions available.
- Add real-schema smoke coverage for the production API surfaces named in #126: models, forecast series, pipeline, flood alerts, and state snapshots.
- Add PostGIS-backed flood spatial coverage for bbox, GeoJSON geometry, and return-period map queries.
- Add worker and Slurm smoke coverage that uses deterministic temporary object-store data and fake Slurm binaries instead of external networks or a real scheduler.
- Document and wire validation layers so fast tests, integration tests, frontend tests, and E2E tests have clear commands and CI ownership.

## Capabilities

### New Capabilities

- `real-integration-test-matrix`: Defines the production-like database, API, spatial, worker, Slurm, and CI validation matrix required for #126.

### Modified Capabilities

- None.

## Impact

- Affects `db/migrations/`, `packages/common/migrate.py`, API routes that query real schema, worker/orchestrator smoke tests, Slurm gateway tests, CI workflow configuration, and developer documentation.
- Adds test and CI behavior, but does not change public API contracts, production database schema semantics, or runtime worker business logic except where required to make existing behavior testable against real services.
