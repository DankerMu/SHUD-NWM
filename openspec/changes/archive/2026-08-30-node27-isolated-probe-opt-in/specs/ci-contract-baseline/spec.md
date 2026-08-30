## MODIFIED Requirements

### Requirement: Integration-owned production sources MUST trigger real-database CI

The CI `database` paths filter SHALL match every production source surface in the finite integration-trigger registry defined by this change: `packages/common/forecast_store.py`, `packages/common/display_coverage.py`, `services/tiles/mvt.py`, `apps/api/routes/hydro_display.py`, `apps/api/main.py`, `scripts/node27_autopipeline.py`, `workers/output_parser/parser.py`, `packages/common/timescale_write_guard.py`, `packages/common/object_store.py`, `packages/common/model_registry.py`, `packages/common/grid_registry_store.py`, `workers/grid_registry/**`, `workers/model_registry/**`, `workers/forcing_producer/**`, and `services/orchestrator/scheduler.py`. The selector contract suite SHALL parse the `database:` filter and mechanically assert that each registered path or tracked member of a registered root matches at least one filter pattern; a workflow change SHALL self-select that contract suite. Matching the filter SHALL open the existing `real-db-integration` job, whose named command has the closed argv shape `pytest -vv -rs -m <one marker expression token>` with no additional options or paths and selects `integration and not timescaledb_210` with its PostgreSQL/Timescale service and dedicated `NHMS_INTEGRATION_DATABASE_URL`: ordinary integration items SHALL execute, while the node-27-only PostgreSQL 15.2 / TimescaleDB 2.10.2 marker SHALL be deselected. The job SHALL expose node-level pass/skip/deselection evidence with pytest `-vv -rs`; this routing change SHALL NOT alter its dedicated DSN, service, job gate, or ordinary integration selection. One full-workflow positive job-contract helper SHALL validate those properties and the named step's effective execution context: step-level environment SHALL NOT replace either integration gate variable, a step condition SHALL NOT disable the command, step/job error-continuation policy SHALL NOT make a failing integration command non-blocking, and direct or inherited custom shell/working-directory metadata SHALL NOT alter the audited root-checkout command semantics. Deletion or effective blanking/opt-out of the dedicated integration context, removal of ordinary `integration`, failure to exclude `timescaledb_210`, or any extra suite-command option/path SHALL produce a named contract violation rather than a green job that runs the wrong oracle set.

#### Scenario: Forecast-store-only diff opens the parity oracle lane

- **WHEN** a non-draft PR changes only `packages/common/forecast_store.py`
- **THEN** the `changes` job reports `database=true`, `SQL Migration Dry Run` runs, and all seven tests in `tests/test_display_coverage_residual_debt_integration.py` appear as executed `PASSED` node IDs rather than skips

#### Scenario: Integration source registry and filter cannot drift silently

- **WHEN** any registered source path no longer matches a `database` pattern, including a mutation that removes the `packages/common/forecast_store.py` pattern
- **THEN** `tests/test_select_ci_tests.py` fails and names the uncovered source

#### Scenario: Dedicated integration DSN cannot disappear silently

- **WHEN** the `real-db-integration` workflow block loses `NHMS_INTEGRATION_DATABASE_URL` while retaining generic `DATABASE_URL`, `NHMS_RUN_INTEGRATION`, and the real-database pytest command
- **THEN** the same positive job-contract helper used for the live workflow reports a violation naming `NHMS_INTEGRATION_DATABASE_URL`, because the integration fixture ignores generic `DATABASE_URL` without an explicit compatibility flag

#### Scenario: Named integration step cannot override or bypass its job contract

- **WHEN** a valid workflow mutation adds a step-level opt-out or blank dedicated DSN, disables the named integration step with its `if`, enables step/job `continue-on-error`, or sets direct/inherited custom shell or working-directory metadata
- **THEN** the same full-workflow structured positive helper used for the live job reports a named effective-environment, execution-condition, fail-closed-policy, shell, or working-directory violation

#### Scenario: Workflow changes execute the trigger contract

- **WHEN** `.github/workflows/ci.yml` changes
- **THEN** targeted selection includes `tests/test_select_ci_tests.py`, so the database-filter and real-DB job contracts execute on that PR

#### Scenario: Generic SQL lane excludes only the node-27 engine marker

- **WHEN** the live workflow's real-database command is parsed
- **THEN** its marker expression requires `integration` and excludes `timescaledb_210`, while the dedicated DSN, PostgreSQL/Timescale service, real-DB job gate, suite root and fail-closed step policy remain unchanged

#### Scenario: Marker-expression drift is rejected

- **WHEN** a workflow mutation restores bare `-m integration`, removes ordinary `integration`, or excludes a broader marker set that hides generic integration coverage
- **THEN** the full-workflow job-contract helper reports the marker-selection violation by name

#### Scenario: Named suite command cannot narrow or empty the generic lane

- **WHEN** a workflow mutation appends a test path or adds `--collect-only` while retaining a semantically valid marker expression
- **THEN** the same full-workflow job-contract helper reports the named closed suite-command violation

#### Scenario: Existing CI lanes retain their contracts

- **WHEN** the generic real-DB marker expression changes from bare `integration` to `integration and not timescaledb_210`
- **THEN** `unit-test-targeted`, master `unit-test`, frontend/docs/openapi/schema filters, draft gating, dedicated integration DSN, PostgreSQL/Timescale service, real-DB job gate, ordinary integration suite selection and node-27 explicit `timescaledb_210` lane remain unchanged
