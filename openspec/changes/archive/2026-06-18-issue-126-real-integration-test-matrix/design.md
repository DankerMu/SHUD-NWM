## Context

Issue #126 is the final open sub-task of Epic #120 after #121-#125. The repository now includes production API contracts, Slurm templates, publish tiles, frontend production data paths, and migration CI smoke, but most behavior tests still use SQLite, in-memory stores, or mocked gateways. The missing risk is not feature breadth; it is whether the same operations pass against real PostgreSQL/PostGIS/TimescaleDB DDL, production SQL dialect behavior, and realistic worker/gateway boundaries.

Fixture level: expanded

Project profile: other

Change surface:
- GitHub Actions CI, `infra/docker-compose.dev.yml`, and any helper scripts/docs for integration commands.
- `db/migrations/*.sql` and `packages/common.migrate` as exercised by real PostgreSQL.
- API smoke tests for `apps/api/routes/models.py`, `forecast.py`, `pipeline.py`, `flood_alerts.py`, and `state_snapshots.py`.
- Worker smoke paths under `workers/canonical_converter`, `workers/forcing_producer`, `workers/shud_runtime`, `workers/output_parser`, and `workers/flood_frequency`.
- Slurm gateway fake-binary coverage around `services/slurm_gateway` and `infra/sbatch`.

Must preserve:
- Existing fast `uv run pytest -q` behavior must not require Docker, PostgreSQL, MinIO, a real Slurm cluster, or external network credentials.
- Existing CI jobs for lint, OpenAPI validation, schema validation, unit tests, and frontend build/test must remain present.
- Existing SQLite/mocked tests remain valid as fast regression tests.
- Existing production migrations remain idempotent and ordered.

Must add/change:
- A deterministic real-database integration lane must run migrations from zero and validate extensions, schemas, enums, hypertables, indexes, constraints, SRID/geometry behavior, and schema drift.
- Real-schema API smoke must cover models, forecast-series, pipeline, flood-alerts, and state-snapshots using seeded test data.
- Flood spatial smoke must prove bbox filtering, GeoJSON geometry output, and return-period map query behavior on PostGIS.
- Worker composition smoke must cover canonical -> forcing -> SHUD dry-run/runtime mock -> parse -> frequency with a temporary local object store and test data.
- Slurm smoke must cover fake `sbatch`, `sacct`, `scancel`, and `sinfo` binaries, array task parsing, and logs without shell injection or real scheduler dependency.
- CI/developer docs must distinguish fast, integration, frontend, and e2e commands and explain required external services.

## Goals / Non-Goals

**Goals:**
- Make real PostgreSQL/PostGIS/TimescaleDB regressions visible in CI when services are available.
- Keep local fast tests usable without external services.
- Provide issue-level evidence for every selected #126 acceptance criterion.

**Non-Goals:**
- No production data volume load testing or national-scale performance benchmark.
- No real Slurm cluster, real MinIO/S3, real GFS/ERA5/IFS network download, or external credential requirement.
- No redesign of API contracts, schemas, or worker algorithms beyond fixes needed for real integration correctness.
- No frontend visual redesign; frontend checks remain existing build/test/E2E lanes unless implementation needs a narrow update.

## Decisions

1. Use pytest markers/environment gates for real integration tests.
   - Rationale: default fast tests stay self-contained, while CI can opt in with `NHMS_RUN_INTEGRATION=1` and `NHMS_INTEGRATION_DATABASE_URL`.
   - Generic `DATABASE_URL` is not used for destructive integration database create/drop setup unless `NHMS_ALLOW_DATABASE_URL_INTEGRATION=1` is set for a guarded compatibility run.
   - Alternative considered: always start Docker from pytest. Rejected because it makes local fast tests slow and brittle.

2. Reuse the existing TimescaleDB HA image and migration entrypoint.
   - Rationale: `.github/workflows/ci.yml` and `infra/docker-compose.dev.yml` already define the production-like database service.
   - Alternative considered: add a separate testcontainers dependency. Rejected unless needed, because it adds packaging and Docker-in-Docker complexity.

3. Seed minimal deterministic records for API and spatial smoke instead of relying on full demo seed only.
   - Rationale: smoke tests should make expected rows explicit and isolate API requirements.
   - Alternative considered: only run `db.seeds.seed_demo`. Rejected because it can hide missing query preconditions and makes failures harder to localize.

4. Use fake Slurm binaries on `PATH` for real gateway smoke.
   - Rationale: it exercises subprocess command construction, parsing, array tasks, cancellation, queue health, and log paths without a real scheduler.
   - Alternative considered: monkeypatch `subprocess.run` only. Rejected as insufficient for #126 because it bypasses executable discovery and command boundary behavior.

## Risk Packs Considered

- Public API / CLI / script entry: selected - API smoke and worker/Slurm CLI-adjacent commands are explicit issue scope.
- Config / project setup: selected - CI services, env vars, pytest markers, and docker-compose usage are in scope.
- File IO / path safety / overwrite: selected - temporary object-store data, Slurm logs, runtime outputs, and worker artifacts are in scope.
- Schema / columns / units / field names: selected - migrations, enums, constraints, API rows, and schema drift checks are central to #126.
- Geospatial / CRS / shapefile sidecars: selected - PostGIS geometry, SRID, bbox, and GeoJSON output are required.
- Time series / forcing / temporal boundaries: selected - forecast-series, forcing rows, flood valid_time, and state snapshots are required.
- Numerical stability / conservation / NaN: not selected - no solver numeric validation beyond dry-run/runtime mock is requested.
- Solver runtime / performance / threading: not selected - no real SHUD execution or threading benchmark is in scope.
- Resource limits / large input / discovery: selected - integration lanes must remain bounded and avoid external downloads or huge fixtures.
- Legacy compatibility / examples: selected - existing fast tests, demo seed, docker-compose, and production templates must continue working.
- Error handling / rollback / partial outputs: selected - migration/idempotency failures, skipped external-service tests, and Slurm command errors need deterministic behavior.
- Release / packaging / dependency compatibility: selected - CI/dev dependency and command layering must remain Linux-compatible.
- Documentation / migration notes: selected - #126 explicitly asks for clear test matrix commands and external service boundaries.

## Risks / Trade-offs

- Real integration tests can be slower than unit tests -> keep them in a marked/layered command and run them in CI with a service database.
- TimescaleDB/PostGIS image startup can be flaky -> use health checks and fail with actionable setup messages instead of silent skips in CI.
- Minimal seed data can drift from production demo data -> assert both structural metadata and representative API behavior.
- Fake Slurm binaries can miss scheduler semantics -> cover command construction/parsing/log boundaries while documenting real cluster execution as a non-goal.

## Migration Plan

1. Add fixture, tests, helper code, and docs on a feature branch.
2. Run fast checks locally.
3. Run the integration lane against the configured PostgreSQL/PostGIS/TimescaleDB service where available.
4. CI runs the integration lane after migrations and seed smoke.
5. Rollback is removal of the new tests/docs/CI lane; no production schema migration is introduced by this change.

## Open Questions

- None for implementation; if the CI image lacks a needed extension, implementation should fix the service image or mark the missing extension as a hard integration failure rather than downgrade coverage.
