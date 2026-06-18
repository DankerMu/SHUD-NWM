# real-integration-test-matrix Specification

## Purpose
TBD - created by archiving change issue-126-real-integration-test-matrix. Update Purpose after archive.
## Requirements
### Requirement: Real database migrations are verified

The system SHALL provide an integration test lane that applies all `db/migrations/*.sql` files from an empty PostgreSQL database with PostGIS and TimescaleDB available, then verifies schema metadata and idempotency.

#### Scenario: Fresh real database migration

- **WHEN** the integration lane runs with `NHMS_RUN_INTEGRATION=1` and a real PostgreSQL/PostGIS/TimescaleDB `NHMS_INTEGRATION_DATABASE_URL`
- **THEN** every migration is applied in filename order, required extensions/schemas/enums/tables/indexes/constraints exist, PostGIS geometry columns have the expected SRID/type, Timescale hypertables exist where required, and a second migration pass skips already applied migrations without error

#### Scenario: Integration database unavailable in fast local tests

- **WHEN** the normal fast test command runs without `NHMS_RUN_INTEGRATION=1`
- **THEN** external-service integration tests are skipped or excluded intentionally, and fast unit/API tests continue to run without Docker or PostgreSQL

### Requirement: Real-schema API smoke covers production surfaces

The system SHALL provide real-schema API smoke tests for models, forecast-series, pipeline, flood-alerts, and state-snapshots using deterministic seeded data.

#### Scenario: Core API smoke succeeds on real schema

- **WHEN** seeded records exist in the migrated real database for a model, river segment, hydro run, pipeline jobs, flood results, and a state snapshot
- **THEN** API requests for model listing/active-model discovery, river forecast-series, pipeline status/stages/jobs, flood alert summary/ranking/timeline/map, and state snapshot list/detail return successful responses with expected identifiers and data fields

### Requirement: Flood spatial queries use PostGIS behavior

The system SHALL verify flood map and segment queries against real PostGIS geometry operations instead of SQLite-only approximations.

#### Scenario: Bbox and GeoJSON filtering

- **WHEN** flood return-period records contain river segment geometries inside and outside a requested bbox
- **THEN** the flood map/GeoJSON query returns only matching features, preserves `FeatureCollection` geometry output, and keeps return-period properties aligned with the selected run, duration, and valid time

### Requirement: Worker chain smoke is deterministic

The system SHALL provide a bounded worker integration smoke that exercises canonical conversion, forcing production, SHUD dry-run or runtime mock, output parsing, and flood frequency calculation using temporary local object-store data.

#### Scenario: Worker composition produces durable artifacts

- **WHEN** the worker smoke runs with synthetic input products and a temporary object store
- **THEN** each stage writes its expected manifest/artifact records, downstream stages consume those artifacts, and the final frequency output can be queried without external network, real S3, or real SHUD solver execution

### Requirement: Slurm gateway smoke uses fake binaries

The system SHALL provide real gateway smoke coverage using fake `sbatch`, `sacct`, `scancel`, and `sinfo` binaries on `PATH`.

#### Scenario: Fake Slurm command boundary

- **WHEN** the real Slurm gateway submits, inspects, cancels, and reads logs for a test job or array job through fake binaries
- **THEN** command arguments are shell-safe, job IDs and array task statuses are parsed correctly, queue status is reported, logs are read from the configured workspace, and no real Slurm cluster is required

### Requirement: Validation commands are layered

The repository SHALL document and/or encode separate validation commands for fast backend tests, real integration tests, frontend tests, and E2E tests.

#### Scenario: CI and developer command matrix is explicit

- **WHEN** a developer or CI runner needs to validate the project
- **THEN** it can run a documented fast command without external services, an explicit opt-in integration command with PostgreSQL/PostGIS/TimescaleDB service variables, frontend unit/build commands, and targeted E2E commands without guessing which services are required

