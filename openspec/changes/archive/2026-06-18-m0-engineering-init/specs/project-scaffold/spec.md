# Project Scaffold

Capability: `project-scaffold`
Status: draft
Parent: m0-engineering-init

## ADDED Requirements

### Requirement: Monorepo directory structure follows the canonical layout

The repository MUST contain the following top-level directories and subdirectories so that all subsequent milestones (M1-M6) can add code without restructuring. Each directory MUST contain at least a placeholder file (`.gitkeep` or `__init__.py`) to ensure it is tracked by version control.

#### Scenario: Full directory tree exists after scaffold generation

WHEN the scaffold generator has completed
THEN the working tree MUST contain every one of the following paths:
  - `apps/api/` -- FastAPI application entry point
  - `apps/web/` -- frontend application (placeholder for M1+)
  - `services/orchestrator/` -- pipeline orchestration service
  - `services/slurm-gateway/` -- Slurm Gateway service (mock in M0)
  - `services/tile-publisher/` -- tile publishing service (placeholder)
  - `workers/data_adapters/` -- data ingestion worker (placeholder)
  - `workers/canonical_converter/` -- canonical conversion worker (placeholder)
  - `workers/forcing-producer/` -- forcing production worker (placeholder)
  - `workers/output-parser/` -- SHUD output parser worker (placeholder)
  - `workers/flood-frequency/` -- flood frequency computation worker (placeholder)
  - `packages/common/` -- shared utilities, error codes, ID generators
  - `schemas/` -- JSON Schema files for manifest, status, qc, job (root-level directory)
  - `db/migrations/` -- ordered SQL migration files
  - `db/seeds/` -- demo and test seed scripts
  - `openapi/` -- OpenAPI contract YAML files
  - `infra/` -- Docker Compose and infrastructure config
  - `tests/` -- integration and end-to-end test suites
AND each directory MUST contain at least one tracked file

#### Scenario: Placeholder directories do not contain business logic

WHEN a developer inspects any worker or placeholder service directory
THEN it MUST contain only structural files (`.gitkeep`, `__init__.py`, or a single-line docstring module)
AND it MUST NOT contain any functional business code
AND a comment or docstring MUST indicate which milestone will implement the module

### Requirement: Makefile provides standard development targets

A root `Makefile` MUST expose the following targets so that `make <target>` is the single entry point for all development workflows. Targets MUST fail with a non-zero exit code on error.

#### Scenario: `make dev` starts the full local development stack

WHEN a developer runs `make dev` in the repository root
THEN Docker Compose MUST start PostgreSQL with PostGIS and TimescaleDB extensions, and MinIO
AND the FastAPI application MUST start in reload mode on a configurable port (default 8000)
AND the developer MUST be able to reach `http://localhost:8000/docs` within 30 seconds
AND stdout MUST print the URLs of all running services

#### Scenario: `make dev` starts mock Slurm Gateway

WHEN a developer runs `make dev` with `SLURM_GATEWAY_BACKEND=mock` (the default)
THEN the mock Slurm Gateway service MUST start alongside other infrastructure services
AND the mock MUST accept job submission requests on its configured port and return canned responses
AND stdout MUST include the mock Slurm Gateway URL in the list of running services

#### Scenario: `make migrate` applies all pending database migrations

WHEN a developer runs `make migrate`
THEN all SQL migration files in `db/migrations/` MUST be executed in filename order against the local PostgreSQL instance
AND the command MUST be idempotent -- running it twice in succession produces no errors and no duplicate objects
AND stdout MUST report the number of migrations applied and the number already applied

#### Scenario: `make reset-db` destroys and recreates the database from scratch

WHEN a developer runs `make reset-db`
THEN the target database MUST be dropped (if it exists) and recreated
AND all migrations MUST be re-applied from `000001` through the latest file
AND all seed data MUST be re-inserted via the seed scripts
AND the command MUST succeed even if the database does not yet exist

#### Scenario: `make seed-demo` inserts demo data

WHEN a developer runs `make seed-demo` after `make migrate`
THEN the seed scripts in `db/seeds/` MUST execute successfully
AND at least one basin, one basin_version, one model_instance, one met_station, one river_segment, and one hydro_run record MUST be queryable from the database
AND running `make seed-demo` a second time MUST NOT produce duplicate-key errors (upsert semantics)

#### Scenario: `make test` runs the full test suite

WHEN a developer runs `make test`
THEN pytest MUST execute all tests under `tests/`
AND the exit code MUST be 0 if all tests pass, non-zero otherwise
AND test output MUST include a summary line with pass/fail/skip counts

#### Scenario: `make lint` checks code quality

WHEN a developer runs `make lint`
THEN the linter MUST check all Python source files with `ruff` (or configured equivalent)
AND the OpenAPI spec MUST be validated with `openapi-spec-validator` or equivalent
AND the exit code MUST be non-zero if any lint violation or validation error is found

### Requirement: Docker Compose dev environment provides all infrastructure dependencies

The file `infra/docker-compose.dev.yml` MUST define services that give developers a fully functional local environment without installing PostgreSQL, TimescaleDB, PostGIS, or MinIO natively.

#### Scenario: PostgreSQL service includes PostGIS and TimescaleDB extensions

WHEN Docker Compose starts the `db` service
THEN the container image MUST be `timescale/timescaledb-ha` or equivalent image that bundles PostgreSQL 15+, PostGIS 3.4+, and TimescaleDB 2.x
AND the service MUST expose port 5432 (or a configurable host port)
AND the database MUST accept connections with the credentials defined in environment variables
AND running `SELECT PostGIS_Version()` MUST return a version string
AND running `SELECT extversion FROM pg_extension WHERE extname = 'timescaledb'` MUST return a version string

#### Scenario: MinIO service provides S3-compatible object storage

WHEN Docker Compose starts the `minio` service
THEN MinIO MUST be accessible on port 9000 (API) and 9001 (console)
AND a default bucket named `nhms` MUST be created on first startup (via init script or entrypoint)
AND the MinIO credentials MUST be configurable via environment variables

#### Scenario: Docker Compose volumes persist data across restarts

WHEN a developer stops and restarts Docker Compose without `--volumes`
THEN PostgreSQL data and MinIO objects MUST survive the restart
AND no migration re-execution or re-seeding is required

#### Scenario: Docker Compose can be fully torn down

WHEN a developer runs `docker compose -f infra/docker-compose.dev.yml down --volumes`
THEN all containers, networks, and named volumes MUST be removed
AND the next `make dev` MUST start from a clean state

### Requirement: Python project configuration is complete and reproducible

The repository MUST contain a `pyproject.toml` at the root (or under `apps/api/`) that defines the Python project metadata, dependencies, and tool configuration.

#### Scenario: pyproject.toml declares all runtime and dev dependencies

WHEN a developer inspects `pyproject.toml`
THEN it MUST declare `python >= 3.11` as the required version
AND runtime dependencies MUST include at minimum: `fastapi`, `uvicorn`, `sqlalchemy`, `asyncpg`, `psycopg2-binary` (or `psycopg`), `alembic`, `pydantic`
AND dev dependencies MUST include at minimum: `pytest`, `pytest-asyncio`, `ruff`, `httpx`
AND all dependency versions MUST use lower-bound pinning (e.g., `>=` or `~=`)

#### Scenario: Virtual environment can be created and activated

WHEN a developer runs `python -m venv .venv && source .venv/bin/activate && pip install -e ".[dev]"`
THEN the installation MUST succeed without errors
AND `python -c "import fastapi; import sqlalchemy; import asyncpg"` MUST succeed
AND `pytest --version` MUST succeed

#### Scenario: pyproject.toml configures ruff linting rules

WHEN a developer runs `ruff check .` from the project root
THEN ruff MUST use the configuration from `pyproject.toml`
AND the rule set MUST include at minimum: E (pycodestyle errors), F (pyflakes), I (isort)
AND line length MUST be configured to 120 characters

### Requirement: Environment configuration uses dotenv with example file

The project MUST provide a `.env.example` file documenting all required environment variables, and the application MUST load configuration from `.env` in development.

#### Scenario: .env.example contains all required variables

WHEN a developer inspects `.env.example`
THEN it MUST contain entries for at minimum:
  - `DATABASE_URL` (PostgreSQL connection string)
  - `S3_ENDPOINT_URL`, `S3_BUCKET_NAME`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`
  - `SLURM_GATEWAY_BACKEND` (with default value `mock`)
  - `API_PORT` (with default value `8000`)
AND each entry MUST include a comment explaining its purpose

#### Scenario: .env is gitignored

WHEN a developer creates `.env` from `.env.example`
THEN `.gitignore` MUST contain an entry for `.env`
AND `git status` MUST NOT show `.env` as an untracked file

#### Scenario: Application starts with .env.example values

WHEN a developer copies `.env.example` to `.env` without modification
AND Docker Compose services are running
THEN `make dev` MUST start the API server successfully using the example values
