# Capability Spec: ci-pipeline

## Context

M0 engineering initialization requires a CI pipeline that validates documentation quality, API contract correctness, JSON Schema validity, database migration integrity, and code quality. The pipeline runs on GitHub Actions and must pass on all pull requests and pushes to main. The migration dry-run must use the same PostgreSQL 15 + PostGIS 3.4 + TimescaleDB 2.x stack as production to catch compatibility issues early.

---

## ADDED Requirements

### Requirement: GitHub Actions workflow configuration

The CI pipeline must be defined as a GitHub Actions workflow file that triggers on the correct events and contains all required jobs.

#### Scenario: Workflow file exists in the correct location

WHEN examining the repository
THEN a workflow file exists at `.github/workflows/ci.yml` (or equivalent name like `ci.yaml`)
AND the file is valid YAML.

#### Scenario: Workflow triggers on push to main

WHEN a commit is pushed to the `main` branch
THEN the CI workflow is triggered
AND all defined jobs execute.

#### Scenario: Workflow triggers on pull requests

WHEN a pull request is opened, synchronized, or reopened targeting any branch
THEN the CI workflow is triggered
AND all defined jobs execute.

#### Scenario: Workflow defines all required jobs

WHEN examining the workflow file
THEN the following jobs are defined:
- `markdown-lint`
- `openapi-validate`
- `json-schema-validate`
- `sql-migration-dry-run`
- `unit-test`
AND each job has a descriptive `name` field.

#### Scenario: All jobs must pass for merge

WHEN any single CI job fails
THEN the overall workflow status is failure
AND the pull request cannot be merged (when branch protection rules are configured)
AND the failed job name and step are visible in the GitHub Actions UI.

---

### Requirement: markdown-lint job

The markdown-lint job must check all documentation files for consistent formatting and style issues.

#### Scenario: markdown-lint scans all docs

WHEN the `markdown-lint` job runs
THEN it lints all `*.md` files under the `docs/` directory
AND it uses a standard Markdown linting tool (e.g., `markdownlint-cli2`, `markdownlint-cli`, or `mdl`).

#### Scenario: markdown-lint uses a configuration file

WHEN examining the repository
THEN a markdownlint configuration file exists (e.g., `.markdownlint.yaml`, `.markdownlint.json`, or `.markdownlint-cli2.yaml`)
AND it defines the enabled/disabled rules
AND commonly noisy rules are configured appropriately (e.g., line length limits for tables and code blocks).

#### Scenario: markdown-lint passes on valid documentation

WHEN all Markdown files conform to the configured rules
THEN the `markdown-lint` job exits with code 0
AND no errors are reported.

#### Scenario: markdown-lint fails on invalid documentation

WHEN a Markdown file contains a linting violation (e.g., inconsistent heading levels, trailing spaces)
THEN the `markdown-lint` job exits with a non-zero code
AND the output identifies the file, line number, and rule that was violated.

#### Scenario: markdown-lint runs in a lightweight environment

WHEN the `markdown-lint` job executes
THEN it runs on a standard GitHub Actions runner (e.g., `ubuntu-latest`)
AND it does not require Docker, PostgreSQL, or Python
AND it completes within 2 minutes for the current documentation volume.

---

### Requirement: openapi-validate job

The openapi-validate job must verify that the OpenAPI specification file is structurally valid and conforms to the OpenAPI 3.x standard.

#### Scenario: openapi-validate checks the API contract

WHEN the `openapi-validate` job runs
THEN it validates `openapi/nhms.v1.yaml`
AND it uses a standard OpenAPI validation tool (e.g., `@redocly/cli`, `swagger-cli`, `openapi-generator-cli validate`, or `spectral`).

#### Scenario: Valid OpenAPI spec passes validation

WHEN `openapi/nhms.v1.yaml` is a valid OpenAPI 3.x document
THEN the `openapi-validate` job exits with code 0.

#### Scenario: Invalid OpenAPI spec fails validation

WHEN `openapi/nhms.v1.yaml` contains structural errors (e.g., missing required fields, invalid references, broken `$ref`)
THEN the `openapi-validate` job exits with a non-zero code
AND the output identifies the specific validation errors.

#### Scenario: OpenAPI validation checks reference integrity

WHEN the OpenAPI spec uses `$ref` to reference component schemas
THEN the validator checks that all referenced schemas exist
AND no dangling references are reported.

---

### Requirement: json-schema-validate job

The json-schema-validate job must validate example JSON files against their corresponding JSON Schema definitions.

#### Scenario: json-schema-validate checks all schemas

WHEN the `json-schema-validate` job runs
THEN it validates all `schemas/*.schema.json` files are valid JSON Schema documents
AND it validates all example files against their corresponding schemas.

#### Scenario: Schema meta-validation passes

WHEN the `json-schema-validate` job runs
THEN each `*.schema.json` file is validated against the JSON Schema meta-schema
AND all four schemas (`run_manifest`, `run_status`, `qc_result`, `pipeline_job`) pass meta-validation.

#### Scenario: Example files pass schema validation

WHEN the `json-schema-validate` job runs
THEN each example file in `schemas/examples/` (or equivalent) is validated against its corresponding schema
AND all example-to-schema validations pass.

#### Scenario: Validation tool is specified

WHEN examining the CI workflow
THEN the json-schema-validate job uses a specific validation tool (e.g., `ajv-cli`, `check-jsonschema`, `jsonschema` Python library, or a custom script)
AND the tool version is pinned or deterministic.

#### Scenario: Invalid example file causes failure

WHEN an example file does not conform to its corresponding schema (e.g., missing required field)
THEN the `json-schema-validate` job exits with a non-zero code
AND the output identifies which file failed, which schema was used, and what the validation error is.

---

### Requirement: sql-migration-dry-run job

The migration dry-run job must execute all SQL migration files against an ephemeral PostgreSQL instance with the same extensions as production, verifying that migrations apply cleanly.

#### Scenario: Ephemeral PostgreSQL is provisioned with correct extensions

WHEN the `sql-migration-dry-run` job runs
THEN it starts or connects to a PostgreSQL 15 instance
AND PostGIS 3.4 extension is available
AND TimescaleDB 2.x extension is available
AND the database instance is ephemeral (created for this job run, destroyed after).

#### Scenario: PostgreSQL uses a service container or Docker image

WHEN examining the CI workflow
THEN the PostgreSQL instance is provisioned via GitHub Actions service container or Docker run step
AND the Docker image includes both PostGIS and TimescaleDB (e.g., `timescale/timescaledb-ha` with PostGIS, or a custom image)
AND the image tag is pinned to match production versions.

#### Scenario: All migrations apply cleanly on empty database

WHEN all migration files under `db/migrations/` are executed in order against the ephemeral database
THEN every migration file executes without SQL errors
AND all 6 schemas are created (core, met, hydro, flood, map, ops)
AND all tables, indexes, and enum types are created
AND all hypertables are created via `create_hypertable` calls.

#### Scenario: Migration execution order is deterministic

WHEN migrations are executed
THEN they are sorted by filename prefix (e.g., `001_`, `002_`, ..., `010_`)
AND the execution order is the same on every CI run
AND migrations that depend on earlier migrations execute after their dependencies.

#### Scenario: Migration dry-run detects SQL errors

WHEN a migration file contains a SQL syntax error or references a non-existent table
THEN the `sql-migration-dry-run` job exits with a non-zero code
AND the output identifies the failing migration file and the SQL error message.

#### Scenario: Migration dry-run tests idempotency

WHEN all migrations have been applied once
AND the migration runner is executed again
THEN no errors are raised (migrations use `IF NOT EXISTS` or the runner skips already-applied migrations)
AND the job still exits with code 0.

#### Scenario: Extensions version matches production

WHEN the `sql-migration-dry-run` job runs
THEN the PostGIS version in the ephemeral database is 3.4.x
AND the TimescaleDB version is 2.x
AND the PostgreSQL major version is 15
AND these versions are documented or asserted in the CI configuration.

---

### Requirement: unit-test job

The unit-test job must run the project's test suite using pytest and produce a coverage report.

#### Scenario: Unit tests run with pytest

WHEN the `unit-test` job runs
THEN it invokes `pytest` to discover and run tests
AND tests are discovered from the standard test directories (e.g., `tests/`)
AND the pytest configuration is defined in `pyproject.toml` or `pytest.ini`.

#### Scenario: Coverage report is generated

WHEN the `unit-test` job runs
THEN it generates a test coverage report using `pytest-cov` or equivalent
AND the coverage report is printed to stdout or uploaded as an artifact
AND the report shows per-file and total coverage percentages.

#### Scenario: All tests pass for job success

WHEN all discovered tests pass
THEN the `unit-test` job exits with code 0.

#### Scenario: Any test failure causes job failure

WHEN one or more tests fail
THEN the `unit-test` job exits with a non-zero code
AND the output identifies the failing test(s) with assertion details.

#### Scenario: Python environment is properly configured

WHEN the `unit-test` job runs
THEN it installs the project Python dependencies (e.g., from `pyproject.toml`, `requirements.txt`, or `requirements-dev.txt`)
AND Python version is 3.11 or later
AND the install step completes without dependency conflicts.

#### Scenario: Test results are visible in GitHub Actions

WHEN the `unit-test` job completes
THEN test results (pass/fail counts) are visible in the GitHub Actions job summary
AND if a test fails, the failure details are shown in the job log without requiring artifact download.

---

### Requirement: Job isolation and independence

CI jobs should be independent where possible to maximize parallelism and minimize feedback time.

#### Scenario: Lightweight jobs run in parallel

WHEN the CI workflow starts
THEN `markdown-lint`, `openapi-validate`, and `json-schema-validate` can run in parallel
AND they do not depend on each other or on heavier jobs.

#### Scenario: Database-dependent jobs use service containers

WHEN the `sql-migration-dry-run` job runs
THEN it uses a GitHub Actions service container for PostgreSQL
AND the service container is scoped to that job only
AND it does not affect other parallel jobs.

#### Scenario: Each job has defined timeout

WHEN examining the workflow file
THEN each job has a `timeout-minutes` setting
AND lightweight jobs (lint, validate) have a timeout of 10 minutes or less
AND heavier jobs (migration, test) have a timeout of 15-30 minutes.

---

### Requirement: CI configuration maintainability

The CI pipeline must be maintainable and extensible for future milestones.

#### Scenario: Tool versions are pinned

WHEN examining the CI workflow
THEN Node.js version (for JS-based linters) is pinned via `actions/setup-node` with a specific version
AND Python version is pinned via `actions/setup-python` with a specific version
AND Docker image tags for PostgreSQL/TimescaleDB are pinned to specific versions, not `latest`.

#### Scenario: Common setup steps are reusable

WHEN examining the CI workflow
THEN common steps (checkout, language setup, dependency install) use established GitHub Actions (e.g., `actions/checkout@v4`)
AND action versions are pinned to a major version or SHA.

#### Scenario: Adding a new CI job is straightforward

WHEN a future milestone needs a new CI job (e.g., integration tests, E2E tests)
THEN the existing workflow structure makes it clear where to add the new job
AND the new job can be added without modifying existing jobs.

#### Scenario: Secrets and credentials are handled securely

WHEN the CI pipeline requires credentials (e.g., for database access in migration dry-run)
THEN credentials are either hardcoded as non-sensitive test values (for ephemeral containers)
OR sourced from GitHub Actions secrets
AND no production credentials appear in the workflow file.
