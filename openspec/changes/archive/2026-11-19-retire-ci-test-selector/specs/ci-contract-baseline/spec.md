## REMOVED Requirements

### Requirement: Targeted CI selection MUST include a changed script's same-name test suite

**Reason**: The PR test selector is retired (#3219); pull requests run the full sharded suite, so there is no route to specify.
**Migration**: None; the full suite runs every test file on every non-frontend pull request.

### Requirement: Targeted CI selection MUST include the container contract's dependent suites

**Reason**: The PR test selector is retired (#3219); pull requests run the full sharded suite, so there is no route to specify.
**Migration**: None; the full suite runs every test file on every non-frontend pull request.

### Requirement: Empty targeted-test selection MUST be loudly self-identifying

**Reason**: The PR test selector is retired (#3219); pull requests run the full sharded suite, so there is no route to specify.
**Migration**: None; the full suite runs every test file on every non-frontend pull request.

### Requirement: Guarded-module selector rules MUST cover their non-gated importer closure

**Reason**: The PR test selector is retired (#3219); pull requests run the full sharded suite, so there is no route to specify.
**Migration**: None; the full suite runs every test file on every non-frontend pull request.

### Requirement: Changed-test PRs MUST run the selector meta-guards

**Reason**: The PR test selector is retired (#3219); pull requests run the full sharded suite, so there is no route to specify.
**Migration**: None; the full suite runs every test file on every non-frontend pull request.

### Requirement: CI paths-filters MUST NOT carry dead file patterns

**Reason**: Its only enforcement was a test in the retired selector suite and its subject is the workflow's own text (#3219, D6); it is removed rather than kept as unenforced prose.
**Migration**: None; the workflow is validated by actionlint and by running.

### Requirement: Selector path-rule duplicate patterns MUST be allowlisted decisions

**Reason**: The PR test selector is retired (#3219); pull requests run the full sharded suite, so there is no route to specify.
**Migration**: None; the full suite runs every test file on every non-frontend pull request.

### Requirement: Selector gating-marker exclusions MUST anchor to the conftest auto-skip set

**Reason**: The PR test selector is retired (#3219); pull requests run the full sharded suite, so there is no route to specify.
**Migration**: None; the full suite runs every test file on every non-frontend pull request.

### Requirement: Directory-rule importer gaps MUST be dispositioned as selections or reasoned exclusions

**Reason**: The PR test selector is retired (#3219); pull requests run the full sharded suite, so there is no route to specify.
**Migration**: None; the full suite runs every test file on every non-frontend pull request.

### Requirement: Support-module changes MUST select their non-gated importer suites

**Reason**: The PR test selector is retired (#3219); pull requests run the full sharded suite, so there is no route to specify.
**Migration**: None; the full suite runs every test file on every non-frontend pull request.

### Requirement: Meta-guard tree derivations MUST stay content-faithful under parse caching

**Reason**: The PR test selector is retired (#3219); pull requests run the full sharded suite, so there is no route to specify.
**Migration**: None; the full suite runs every test file on every non-frontend pull request.

### Requirement: Shell wrapper changes MUST gate their guard suites

**Reason**: The PR test selector is retired (#3219); pull requests run the full sharded suite, so there is no route to specify.
**Migration**: None; the full suite runs every test file on every non-frontend pull request.

### Requirement: CI workflow policy changes MUST execute their contract suite

**Reason**: The PR test selector is retired (#3219); pull requests run the full sharded suite, so there is no route to specify.
**Migration**: None; the full suite runs every test file on every non-frontend pull request.

### Requirement: OpenAPI changes MUST execute drift and generated-type assertions

**Reason**: Replaced by "Generated frontend types MUST match the static OpenAPI schema": the selector scenarios of this requirement are cut (#3219), and OpenSpec refuses a MODIFIED block that drops a scenario, so the surviving text moves to a new header.
**Migration**: See the ADDED requirement "Generated frontend types MUST match the static OpenAPI schema"; apart from the selector cuts its body and scenarios are unchanged.

### Requirement: Shared-library targeted selection MUST retain its baseline

**Reason**: The PR test selector is retired (#3219); pull requests run the full sharded suite, so there is no route to specify.
**Migration**: None; the full suite runs every test file on every non-frontend pull request.

### Requirement: Tree-scanning invariant suites MUST follow every scanned source root

**Reason**: The PR test selector is retired (#3219); pull requests run the full sharded suite, so there is no route to specify.
**Migration**: None; the full suite runs every test file on every non-frontend pull request.

### Requirement: Selector-development changes MUST retain full-tree collection smoke

**Reason**: The PR test selector is retired (#3219); pull requests run the full sharded suite, so there is no route to specify.
**Migration**: None; the full suite runs every test file on every non-frontend pull request.

### Requirement: Integration-owned production sources MUST trigger real-database CI

**Reason**: Replaced by "Integration-owned production sources MUST open the real-database CI job": the selector scenarios of this requirement are cut (#3219), and OpenSpec refuses a MODIFIED block that drops a scenario, so the surviving text moves to a new header. The job-contract helper sentences and their four scenarios are cut as well: the helper lived only in the deleted selector suite and is not ported.
**Migration**: See the ADDED requirement "Integration-owned production sources MUST open the real-database CI job"; apart from the selector cuts and the job-contract helper text its body and scenarios are unchanged.

### Requirement: Changed test suites MUST select their non-gated top-level importer suites

**Reason**: The PR test selector is retired (#3219); pull requests run the full sharded suite, so there is no route to specify.
**Migration**: None; the full suite runs every test file on every non-frontend pull request.

### Requirement: Calibration declaration changes MUST execute their consumer contract suites

**Reason**: The PR test selector is retired (#3219); pull requests run the full sharded suite, so there is no route to specify.
**Migration**: None; the full suite runs every test file on every non-frontend pull request.

### Requirement: node-27 mvt-cache-retention unit changes MUST reach the sibling systemd.err lane set pin

**Reason**: The PR test selector is retired (#3219); pull requests run the full sharded suite, so there is no route to specify.
**Migration**: None; the full suite runs every test file on every non-frontend pull request.

### Requirement: every node-27 service unit change MUST reach the sibling systemd.err lane set pin

**Reason**: The PR test selector is retired (#3219); pull requests run the full sharded suite, so there is no route to specify.
**Migration**: None; the full suite runs every test file on every non-frontend pull request.

### Requirement: node-27 unit files with a content-asserting owner suite MUST select that suite

**Reason**: The PR test selector is retired (#3219); pull requests run the full sharded suite, so there is no route to specify.
**Migration**: None; the full suite runs every test file on every non-frontend pull request.

### Requirement: precip service tree changes MUST select the prewarm reader suite

**Reason**: The PR test selector is retired (#3219); pull requests run the full sharded suite, so there is no route to specify.
**Migration**: None; the full suite runs every test file on every non-frontend pull request.

### Requirement: display and scheduler unit files with a content-asserting owner suite MUST select that suite

**Reason**: The PR test selector is retired (#3219); pull requests run the full sharded suite, so there is no route to specify.
**Migration**: None; the full suite runs every test file on every non-frontend pull request.

### Requirement: the scheduler refresh env template MUST select its content-asserting owner suite

**Reason**: The PR test selector is retired (#3219); pull requests run the full sharded suite, so there is no route to specify.
**Migration**: None; the full suite runs every test file on every non-frontend pull request.

### Requirement: the precipitation application-composition owners MUST select the precipitation surface oracles

**Reason**: The PR test selector is retired (#3219); pull requests run the full sharded suite, so there is no route to specify.
**Migration**: None; the full suite runs every test file on every non-frontend pull request.

### Requirement: the precipitation composition edges MUST be backed by constructive mutation proofs

**Reason**: Its only justification was a selector edge ("A selector edge that pulls a suite into the lane is only justified if ..."), and the PR test selector is retired (#3219).
**Migration**: None; the mutation proofs in `tests/test_precip_overlay.py` and `tests/test_openapi_drift.py` stay as ordinary tests and run in the full suite.

### Requirement: the river-segment routed root set MUST be derived from the scan's own source, not frozen a second time

**Reason**: The PR test selector is retired (#3219); pull requests run the full sharded suite, so there is no route to specify.
**Migration**: None; the full suite runs every test file on every non-frontend pull request.

### Requirement: the retention copyback mutex load-bearing modules MUST select the mutex suite

**Reason**: The PR test selector is retired (#3219); pull requests run the full sharded suite, so there is no route to specify.
**Migration**: None; the full suite runs every test file on every non-frontend pull request.

### Requirement: path routing rules MUST NOT carry an activation gate

**Reason**: The PR test selector is retired (#3219); pull requests run the full sharded suite, so there is no route to specify.
**Migration**: None; the full suite runs every test file on every non-frontend pull request.

### Requirement: the supplemental invariant-root meta-guard MUST observe routing, not only the constant

**Reason**: The PR test selector is retired (#3219); pull requests run the full sharded suite, so there is no route to specify.
**Migration**: None; the full suite runs every test file on every non-frontend pull request.

### Requirement: registry partition additions MUST be registered, never backfilled

**Reason**: The frozen partition-oracle fixtures and ledgers that enforced this requirement are deleted with the selector suite, their only reader (#3219, D4); the requirement is removed, not ported.
**Migration**: None; the registry-import partitions stay and run in the full suite on every non-frontend pull request.

### Requirement: A selector rule naming consumers it cannot derive SHALL carry an independent exact-set anchor

**Reason**: The PR test selector is retired (#3219); pull requests run the full sharded suite, so there is no route to specify.
**Migration**: None; the full suite runs every test file on every non-frontend pull request.

### Requirement: The OpenAPI patch module's rule SHALL reach every suite that can observe its output, and SHALL NOT carry suites that cannot

**Reason**: The PR test selector is retired (#3219); pull requests run the full sharded suite, so there is no route to specify.
**Migration**: None; the full suite runs every test file on every non-frontend pull request.

### Requirement: The cross-PR review-gate memory file SHALL select its structural guard

**Reason**: After the selector clauses are cut its only remaining obligation is that the memory file opens the `backend` lane; that filter is deleted (#3219) and every diff with a path outside `apps/frontend/` runs the full suite.
**Migration**: None; the memory file's structural guard suite runs in the full suite on every non-frontend pull request.

### Requirement: the scheduler runtime rule site MUST select the extra-roots wiring suite

**Reason**: The PR test selector is retired (#3219); pull requests run the full sharded suite, so there is no route to specify.
**Migration**: None; the full suite runs every test file on every non-frontend pull request.

### Requirement: the file-orchestration migration rule site MUST select the rollback-lane writer suites

**Reason**: The PR test selector is retired (#3219); pull requests run the full sharded suite, so there is no route to specify.
**Migration**: None; the full suite runs every test file on every non-frontend pull request.

### Requirement: frozen fixture data read by a non-gated suite MUST select that suite

**Reason**: The PR test selector is retired (#3219); pull requests run the full sharded suite, so there is no route to specify.
**Migration**: None; the full suite runs every test file on every non-frontend pull request.

### Requirement: production-topology scanner inputs MUST select the topology hard gate

**Reason**: The PR test selector is retired (#3219); pull requests run the full sharded suite, so there is no route to specify.
**Migration**: None; the full suite runs every test file on every non-frontend pull request.

### Requirement: new forcing-template modules MUST select the forcing shape oracles

**Reason**: The PR test selector is retired (#3219); pull requests run the full sharded suite, so there is no route to specify.
**Migration**: None; the full suite runs every test file on every non-frontend pull request.

### Requirement: review-governance tools and records SHALL select their suites

**Reason**: The PR test selector is retired (#3219); pull requests run the full sharded suite, so there is no route to specify.
**Migration**: None; the full suite runs every test file on every non-frontend pull request.

### Requirement: SHUD executable preflight changes MUST select its behaviour suites

**Reason**: The PR test selector is retired (#3219); pull requests run the full sharded suite, so there is no route to specify.
**Migration**: None; the full suite runs every test file on every non-frontend pull request.

### Requirement: Irregular source and package routes MUST select their owned state-clone and mapping-builder suites

**Reason**: The PR test selector is retired (#3219); pull requests run the full sharded suite, so there is no route to specify.
**Migration**: None; the full suite runs every test file on every non-frontend pull request.

## ADDED Requirements

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
