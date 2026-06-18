# Capability Spec: object-storage-layout

## Context

The system uses S3-compatible object storage (MinIO for local development, S3 for production) to persist raw meteorological data, canonical products, forcing packages, model binaries, state snapshots, run I/O, and map tiles. The prefix convention is defined in `docs/spec/01_architecture_and_flow.md` section 7. M0 must configure MinIO in Docker Compose, enforce the prefix convention via a validation utility, and seed example objects so that developers can verify the layout.

---

## ADDED Requirements

### Requirement: MinIO service in Docker Compose

The local development environment must include a MinIO instance configured in `infra/docker-compose.dev.yml` with a default bucket and access credentials.

#### Scenario: MinIO starts with Docker Compose

WHEN a developer runs `docker compose -f infra/docker-compose.dev.yml up`
THEN a MinIO container starts and is accessible on a documented port (e.g., 9000 for API, 9001 for console)
AND the MinIO health check passes within 30 seconds.

#### Scenario: Default bucket is created on startup

WHEN the MinIO container starts for the first time
THEN a bucket named `nhms` is automatically created
AND the bucket creation uses an init container, entrypoint script, or MinIO client (`mc`) sidecar
AND no manual bucket creation is required by the developer.

#### Scenario: Access credentials are configured via environment variables

WHEN examining `infra/docker-compose.dev.yml`
THEN MinIO root credentials are set via environment variables (e.g., `MINIO_ROOT_USER`, `MINIO_ROOT_PASSWORD`)
AND these credentials are documented in a `.env.example` or the project README
AND the default dev credentials are non-production values (e.g., `minioadmin`/`minioadmin`).

#### Scenario: MinIO data is persisted across container restarts

WHEN a developer stops and restarts the Docker Compose stack
THEN previously uploaded objects in the `nhms` bucket are still present
AND MinIO uses a named Docker volume for data persistence.

---

### Requirement: Object storage prefix convention enforcement

The system must define the canonical prefix layout matching `docs/spec/01_architecture_and_flow.md` section 7, and provide a validation utility that checks whether a given S3 path conforms to the convention.

#### Scenario: Prefix convention covers all defined path patterns

WHEN examining the prefix convention definition
THEN the following prefixes are defined and documented:
```
raw/{source}/{cycle_time}/
canonical/{source}/{cycle_time}/{variable}/
forcing/{source}/{cycle_time}/{basin_version_id}/{model_id}/
models/{model_id}/
states/{model_id}/{valid_time}/
runs/{run_id}/input/
runs/{run_id}/output/
runs/{run_id}/logs/
tiles/met/{product_id}/
tiles/hydro/{run_id}/
```
AND each prefix pattern has a brief description of what objects it contains.

#### Scenario: Prefix validator accepts both bare keys and full S3 URIs

WHEN calling the prefix validation utility
THEN it MUST accept bare keys (e.g., `raw/gfs/2026050100/gfs_t2m.grib2`)
AND it MUST accept full S3 URIs (e.g., `s3://nhms/raw/gfs/2026050100/gfs_t2m.grib2`)
AND in both cases the validation result and extracted components MUST be identical
AND the bucket name in a full URI is stripped before prefix matching.

#### Scenario: Valid path passes prefix validation

WHEN calling the prefix validation utility with a path like `raw/gfs/2026050100/gfs_t2m.grib2`
THEN the utility returns success (valid)
AND the parsed prefix category is `raw`
AND extracted components include `source=gfs` and `cycle_time=2026050100`.

#### Scenario: Valid forcing path passes prefix validation

WHEN calling the prefix validation utility with a path like `forcing/gfs/2026050100/yangtze_v2026_01/yangtze_shud_v12/forcing_package.tar.gz`
THEN the utility returns success (valid)
AND extracted components include `source=gfs`, `cycle_time=2026050100`, `basin_version_id=yangtze_v2026_01`, `model_id=yangtze_shud_v12`.

#### Scenario: Valid run path passes prefix validation

WHEN calling the prefix validation utility with a path like `runs/fcst_gfs_2026050100_yangtze_shud_v12/output/rivqdown.csv`
THEN the utility returns success (valid)
AND extracted components include `run_id=fcst_gfs_2026050100_yangtze_shud_v12` and `sub_prefix=output`.

#### Scenario: Invalid path fails prefix validation with clear error

WHEN calling the prefix validation utility with a path like `data/gfs/something.grib2`
THEN the utility returns failure (invalid)
AND the error message indicates that `data` is not a recognized top-level prefix
AND the error lists the valid top-level prefixes.

#### Scenario: Path with wrong nesting depth fails validation

WHEN calling the prefix validation utility with a path like `forcing/gfs/file.tar.gz`
THEN the utility returns failure (invalid)
AND the error message indicates that the forcing prefix requires `{source}/{cycle_time}/{basin_version_id}/{model_id}/` structure.

#### Scenario: Validation utility is importable as a Python module

WHEN a Python module imports the validation utility (e.g., `from nhms.storage import validate_prefix`)
THEN the function is available and callable
AND it accepts a string path argument and returns a structured result (valid/invalid with details).

---

### Requirement: Seed script creates example objects in correct prefixes

The seed process must upload example (placeholder) objects into MinIO at the correct prefixes so that developers can verify the object storage layout and S3 URI references in the database are resolvable.

#### Scenario: Seed creates example objects in `models/` prefix

WHEN `make seed-demo` runs (including the object storage seed component)
THEN an object exists at `s3://nhms/models/yangtze_shud_v12/model_package.tar.gz`
AND the object contains placeholder content (can be a small text file or empty tar).

#### Scenario: Seed creates example objects in `forcing/` prefix

WHEN `make seed-demo` runs
THEN at least one object exists under `s3://nhms/forcing/gfs/2026050100/yangtze_v2026_01/yangtze_shud_v12/`
AND the object key follows the prefix convention.

#### Scenario: Seed creates example objects in `runs/` prefix

WHEN `make seed-demo` runs
THEN objects exist under the following prefixes for the demo run:
- `s3://nhms/runs/fcst_gfs_2026050100_yangtze_shud_v12/input/`
- `s3://nhms/runs/fcst_gfs_2026050100_yangtze_shud_v12/output/`
- `s3://nhms/runs/fcst_gfs_2026050100_yangtze_shud_v12/logs/`
AND each sub-prefix contains at least one placeholder object.

#### Scenario: Seed creates example objects in `states/` prefix

WHEN `make seed-demo` runs
THEN an object exists at `s3://nhms/states/yangtze_shud_v12/2026050100/yangtze_v12.cfg.ic`
AND the path follows the `states/{model_id}/{valid_time}/` convention.

#### Scenario: Seed creates example objects in `raw/` and `canonical/` prefixes

WHEN `make seed-demo` runs
THEN at least one object exists under `s3://nhms/raw/gfs/2026050100/`
AND at least one object exists under `s3://nhms/canonical/gfs/2026050100/t2m/`
AND objects follow the prefix convention for their respective categories.

#### Scenario: All database URI fields resolve to existing objects

WHEN `make seed-demo` has completed (both database seed and object storage seed)
THEN every `*_uri` field in the database that references `s3://nhms/` can be resolved to an existing object in MinIO
AND running a verification script that checks all URIs reports zero missing objects.

#### Scenario: Object storage seed is idempotent

WHEN `make seed-demo` runs twice
THEN no errors are raised on the second run
AND objects are overwritten or skipped without failure
AND the object count remains consistent.

---

### Requirement: Prefix convention documentation

The prefix convention must be documented in a format that is both human-readable and machine-parseable.

#### Scenario: Prefix convention is documented in the repository

WHEN a developer looks for the object storage layout documentation
THEN a file exists (e.g., `docs/spec/01_architecture_and_flow.md` section 7 or a dedicated `STORAGE_LAYOUT.md`)
AND it lists all prefix patterns with examples and descriptions.

#### Scenario: Prefix patterns are defined in a machine-readable format

WHEN the validation utility initializes
THEN it reads prefix patterns from a configuration source (Python constants, YAML, or JSON)
AND the patterns match those documented in the architecture spec
AND adding a new prefix pattern requires updating only the configuration, not the validation logic.

---

### Requirement: Integration with application configuration

The application must be able to resolve the MinIO endpoint and bucket name from configuration, supporting both local development and production S3.

#### Scenario: Storage endpoint is configurable via environment variables

WHEN the application starts
THEN it reads the S3 endpoint URL from an environment variable (e.g., `S3_ENDPOINT_URL`)
AND it reads the bucket name from an environment variable (e.g., `S3_BUCKET_NAME`, defaulting to `nhms`)
AND it reads access credentials from environment variables (e.g., `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`).

#### Scenario: Local development uses MinIO endpoint

WHEN the application runs in local development mode with Docker Compose
THEN `S3_ENDPOINT_URL` points to the MinIO container (e.g., `http://minio:9000` or `http://localhost:9000`)
AND the `nhms` bucket is accessible
AND standard S3 SDK operations (put, get, list, delete) work against MinIO.

#### Scenario: Production uses real S3 endpoint

WHEN the application runs in production
THEN `S3_ENDPOINT_URL` is either unset (using default AWS S3) or set to a regional endpoint
AND the same bucket name and prefix convention apply
AND no code changes are required to switch between MinIO and S3.
