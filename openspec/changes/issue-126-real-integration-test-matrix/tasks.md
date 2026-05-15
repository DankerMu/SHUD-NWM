## 1. Real Database Integration

- [x] 1.1 Add an integration marker/env gate so fast `uv run pytest -q` does not require Docker or PostgreSQL.
- [x] 1.2 Add a real PostgreSQL/PostGIS/Timescale migration test that applies migrations from zero, reruns idempotently, and verifies extensions, schemas, enums, hypertables, indexes, constraints, geometry type/SRID, and migration records.
- [x] 1.3 Add a deterministic real-schema seed/helper for the API and spatial smoke tests without depending on external network data.

## 2. API and Spatial Smoke

- [x] 2.1 Add real-schema API smoke for models, river forecast-series, pipeline status/stages/jobs, flood alert summary/ranking/timeline/map, and state snapshot list/detail.
- [x] 2.2 Add PostGIS flood spatial smoke for bbox filtering, GeoJSON `FeatureCollection` output, return-period properties, and inside/outside geometry behavior.
- [x] 2.3 Ensure error output is actionable when the integration database is missing in CI, while local fast tests remain intentionally skipped or unselected.

## 3. Worker and Slurm Smoke

- [x] 3.1 Add a bounded worker composition smoke for canonical -> forcing -> SHUD dry-run/runtime mock -> parse -> frequency using a temporary local object store and synthetic data.
- [x] 3.2 Add real Slurm gateway smoke with fake `sbatch`, `sacct`, `scancel`, and `sinfo` binaries on `PATH`, including array task parsing and log reads.
- [x] 3.3 Preserve existing monkeypatched gateway tests while adding fake-binary coverage at the command boundary.

## 4. CI and Documentation

- [x] 4.1 Wire CI so the real database integration lane runs against the service database after migrations and seed setup.
- [x] 4.2 Document or script the validation matrix: fast backend, integration backend, frontend unit/build, frontend E2E/preview.
- [x] 4.3 Keep Linux/dev setup compatible with `uv run`, existing `uv.lock`, and Corepack/pnpm frontend commands.

## 5. Verification Evidence

- [x] 5.1 `openspec validate issue-126-real-integration-test-matrix --strict --no-interactive`
- [x] 5.2 `uv run pytest -q`
- [x] 5.3 `uv run pytest -q -m integration` is wired for PostgreSQL/PostGIS/TimescaleDB service variables; local no-URL gate was verified and CI provides the real service run
- [x] 5.4 `uv run ruff check .`
- [x] 5.5 `cd apps/frontend && corepack pnpm test`
- [x] 5.6 `cd apps/frontend && corepack pnpm build`
- [x] 5.7 `git diff --check`

## Non-goals

- [x] N.1 No real Slurm cluster, real MinIO/S3, external weather-data download, production-scale load test, or real SHUD numerical benchmark is required for this issue.
- [x] N.2 No public API or database schema contract change is intended unless implementation discovers a real integration defect that must be fixed to satisfy the existing contracts.
