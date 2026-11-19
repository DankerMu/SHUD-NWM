# ci-contract-baseline Specification

## Purpose
Keep the CI contract trustworthy as a merge gate:
- pull requests and master pushes run the full sharded pytest suite, so no change depends on a routing rule to reach its tests;
- the entropy hard gate and the OpenAPI checks stay green without weakening their rules;
- workflow concurrency, path filters and tooling environments never silently drop or misroute a run.

## Requirements

### Requirement: Governance cleanup starts from a green contract baseline

Governance PRs that change role boundaries, dead-code paths, documentation authority, or entropy automation MUST start from a passing master contract baseline. The baseline includes backend fast tests and the generated frontend types matching `openapi/nhms.v1.yaml`.

#### Scenario: generated frontend types drift blocks governance
- **WHEN** `openapi/nhms.v1.yaml` generates TypeScript that differs from `apps/frontend/src/api/types.ts`
- **THEN** the governance baseline is not satisfied and cleanup PRs MUST wait until the generated type artifact is reconciled

#### Scenario: contract tests pass before downstream governance
- **WHEN** `tests/test_api_contract.py` and `tests/test_openapi_drift.py` pass on the current branch
- **THEN** downstream governance changes may use that branch as their baseline

#### Scenario: full backend fast gate passes before downstream governance
- **WHEN** Governance-1 through Governance-4 issues are started without an explicit maintainer waiver
- **THEN** the branch has passing evidence for `uv run pytest -q -m "not e2e and not grib and not integration"` in addition to focused OpenAPI and generated-type checks

#### Scenario: frontend contract generation check passes before downstream governance
- **WHEN** Governance-1 through Governance-4 issues are started without an explicit maintainer waiver
- **THEN** `cd apps/frontend && corepack pnpm run check:api-types` passes or equivalent CI evidence proves generated frontend types match the committed OpenAPI contract

### Requirement: Python tooling commands use the repository-managed environment

Developer entrypoints for backend Python work MUST use `uv run` so local, CI, and production-like checks resolve the same locked environment.

#### Scenario: Makefile test and lint targets use uv
- **WHEN** a developer runs `make test` or `make lint`
- **THEN** the underlying commands run through `uv run pytest` and `uv run ruff check .`

#### Scenario: Makefile app and migration targets use uv
- **WHEN** a developer runs `make dev`, `make migrate`, `make seed-demo`, or `make seed-m1-model`
- **THEN** Python modules are invoked through `uv run python -m ...`

#### Scenario: Makefile reset-db preserves uv-backed child targets
- **WHEN** a developer inspects or runs `make reset-db`
- **THEN** the target preserves the existing database drop/create sequence and invokes `$(MAKE) migrate` and `$(MAKE) seed-demo`, so migration and seed Python modules run through the uv-backed child targets

### Requirement: Entropy hard gate MUST be green on master without weakening the gate

The repository SHALL keep the entropy hard gate
(`build_report(mode="hard-gate")`)
reporting `hard_gate_status == "pass"` on master, and restoring it after
a diagnostic-token finding SHALL change the flagged production-adjacent
text rather than adding checker exemptions, unless the checker itself is
provably wrong.

#### Scenario: diagnostic-token comment reworded

- **WHEN** the entropy hard gate flags a prose comment in
  `workers/shud_runtime/runtime.py` for naming a QHH diagnostic token
- **THEN** the comment is reworded to drop the literal token with zero
  executable-logic change, and
  `tests/test_entropy_audit_report_contract.py::test_entropy_audit_current_repo_hard_gate_has_zero_production_topology_findings` passes with
  `hard_gate_failing_count == 0`

### Requirement: CI concurrency MUST preserve every non-PR workflow run

The CI workflow MUST group pull-request runs by pull-request identity and cancel superseded runs for that same pull request, while every `push` and `workflow_dispatch` run MUST use its own workflow-run identity and MUST NOT cancel or replace another non-PR run that is running or pending.

#### Scenario: Superseded pull-request run is cancelled

- **WHEN** a new CI run starts for the same pull request
- **THEN** both runs resolve to the same PR-scoped concurrency group and `cancel-in-progress` is true for that event

#### Scenario: Master pushes cannot cancel or replace each other

- **WHEN** two `push` events on `master` create separate CI workflow runs
- **THEN** their groups differ by `github.run_id`, `cancel-in-progress` is false, and neither run is removed by this workflow's concurrency policy

#### Scenario: Manual full CI is isolated from master pushes

- **WHEN** a `workflow_dispatch` run overlaps a `push` run
- **THEN** each uses its own `github.run_id` group and neither can cancel or replace the other through this concurrency policy

### Requirement: OpenAPI error validation MUST be green without weakening rules

The exact CI OpenAPI Validate command using pinned Redocly 1.25.13 and only the existing `no-unused-components` skip MUST exit zero with no errors. The repair MUST NOT downgrade the declared dialect, disable `spec`, `security-defined`, or `no-empty-servers`, add a broad config suppression, or replace validation with a warning-only/collect-only check.

#### Scenario: Declared 3.1 contract passes the pinned validator

- **WHEN** CI lints `openapi/nhms.v1.yaml` with the repository command
- **THEN** all dialect, security-defined, and server errors are absent and the command exits zero

#### Scenario: Removing a load-bearing repair leg fails

- **WHEN** a mutation removes null normalization, the same-origin server, root security, or one protected-operation override
- **THEN** a contract test or the pinned Redocly command fails and names the missing invariant

### Requirement: Frontend type check MUST gate the frontend build

The `frontend-build` CI job SHALL execute a full-program TypeScript check (`tsc --noEmit` over the application project) as its own named step, ordered before the bundle build, and that step SHALL fail the job on any type error. The check SHALL NOT be reduced to a warning, folded into another step's command line, or satisfied by `vite build` (which transpiles per file and surfaces no whole-program type errors) or by `check:api-types` (which only diffs regenerated types against the static schema and never compiles application code).

#### Scenario: Type error fails the frontend job

- **WHEN** a pull request touching `apps/frontend/**` introduces a TypeScript error anywhere in the type-checked project
- **THEN** the `frontend-build` job's type-check step MUST fail and the job MUST be red

#### Scenario: The gate is observably executed

- **WHEN** the `frontend-build` job runs on a pull request
- **THEN** the type-check step MUST appear in the run as executed and passed, not skipped and not collapsed into the install/build/test step

#### Scenario: Contract drift between backend and frontend is caught

- **WHEN** a backend change removes or renames a named response schema that a frontend module consumes
- **THEN** regenerating the frontend types and running the gate MUST fail, rather than the stale reference silently degrading to `unknown`

### Requirement: Generated frontend types MUST match the static OpenAPI schema

Generated frontend types MUST match the static schema, and a semantics-preserving dialect repair MUST leave the committed business type artifact byte-identical.

#### Scenario: Generated business types do not drift

- **WHEN** all legacy nullable nodes are re-expressed with equivalent 3.1 unions and security/server metadata is added
- **THEN** `check:api-types` and the Python generated-type byte comparison pass with no change to `apps/frontend/src/api/types.ts`

### Requirement: Integration-owned production sources MUST open the real-database CI job

The CI `database` paths filter SHALL match every production source surface in the finite integration-trigger registry defined by this change: `packages/common/forecast_store.py`, `packages/common/display_coverage.py`, `services/tiles/mvt.py`, `apps/api/routes/hydro_display.py`, `apps/api/main.py`, `scripts/node27_autopipeline.py`, `workers/output_parser/parser.py`, `packages/common/timescale_write_guard.py`, `packages/common/object_store.py`, `packages/common/model_registry.py`, every `packages/common/model_registry_*.py` owner module the #2617 split produced (matched by a `packages/common/model_registry_*.py` pattern kept beside the original literal), `packages/common/grid_registry_store.py`, `workers/grid_registry/**`, `workers/model_registry/**`, `workers/forcing_producer/**`, and `services/orchestrator/scheduler.py`. Matching the filter SHALL open the existing `real-db-integration` job, whose named command has the closed argv shape `pytest -vv -rs -m <one marker expression token>` with no additional options or paths and selects `integration and not timescaledb_210` with its PostgreSQL/Timescale service and dedicated `NHMS_INTEGRATION_DATABASE_URL`: ordinary integration items SHALL execute, while the node-27-only PostgreSQL 15.2 / TimescaleDB 2.10.2 marker SHALL be deselected. The job SHALL expose node-level pass/skip/deselection evidence with pytest `-vv -rs`; this routing change SHALL NOT alter its dedicated DSN, service, job gate, or ordinary integration selection.

#### Scenario: Forecast-store-only diff opens the parity oracle lane

- **WHEN** a non-draft PR changes only `packages/common/forecast_store.py`
- **THEN** the `changes` job reports `database=true`, `SQL Migration Dry Run` runs, and all seven tests in `tests/test_display_coverage_residual_debt_integration.py` appear as executed `PASSED` node IDs rather than skips

#### Scenario: Generic SQL lane excludes only the node-27 engine marker

- **WHEN** the workflow runs
- **THEN** its marker expression requires `integration` and excludes `timescaledb_210`, while the dedicated DSN, PostgreSQL/Timescale service, real-DB job gate, suite root and fail-closed step policy remain unchanged

#### Scenario: Existing CI lanes retain their contracts

- **WHEN** the generic real-DB marker expression changes from bare `integration` to `integration and not timescaledb_210`
- **THEN** `unit-test`, frontend/docs/openapi/schema filters, draft gating, dedicated integration DSN, PostgreSQL/Timescale service, real-DB job gate, ordinary integration suite selection and node-27 explicit `timescaledb_210` lane remain unchanged
