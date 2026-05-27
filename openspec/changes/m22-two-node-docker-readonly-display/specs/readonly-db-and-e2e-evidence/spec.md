## ADDED Requirements

### Requirement: Display readonly DB validation

The display service SHALL be validated with readonly database credentials for display APIs.

#### Scenario: Readonly display smoke
- **WHEN** the API starts with `NHMS_SERVICE_ROLE=display_readonly` and a readonly DB user
- **THEN** read-only display routes such as health, models, stations, latest-product, pipeline status, jobs, and job logs can be exercised
- **AND** no display smoke step requires writing hydro, met, or pipeline terminal state.

#### Scenario: Mutating API blocked with readonly DB
- **WHEN** retry or cancel is called on the display API backed by readonly DB credentials
- **THEN** the request returns `CONTROL_PLANE_MANUAL_ACTION_REQUIRED`
- **AND** it does not fail later because of a database write attempt.

#### Scenario: Readonly DB evidence
- **WHEN** readonly DB validation runs
- **THEN** evidence records the DB role type, `current_user`, redacted database URL, commands, and pass/fail/blocker status
- **AND** secrets are redacted.

#### Scenario: Write privileges denied
- **WHEN** readonly DB validation runs against hydro, met, ops, and pipeline-critical tables
- **THEN** controlled `INSERT`, `UPDATE`, `DELETE`, and DDL probes are denied or rolled back with permission errors
- **AND** a display PASS cannot be claimed by merely labeling a writer credential as readonly.

### Requirement: Docker and E2E evidence location

All project-created Docker smoke, codeagent review, and two-node E2E artifacts SHALL be written under the repository or `/scratch/frd_muziyao`.

#### Scenario: Stage change review artifacts
- **WHEN** OpenSpec review or planning agents produce output
- **THEN** the output is saved under `artifacts/stage-change/m22-two-node-docker-readonly-display/` or `/scratch/frd_muziyao/...`
- **AND** no project-created temporary evidence is written to the system disk by default.

#### Scenario: Docker smoke artifacts
- **WHEN** Docker build, compose config, or container security smoke tests run
- **THEN** test logs and generated evidence are written under the repository `artifacts/` tree or `/scratch/frd_muziyao`
- **AND** commands document any unavoidable Docker daemon cache usage separately.

#### Scenario: Docker disk preflight
- **WHEN** Docker build or compose smoke is about to run
- **THEN** evidence records `docker version`, `docker compose version`, DockerRootDir from `docker info`, `docker system df`, and relevant `df -h`
- **AND** low available space marks Docker smoke as `BLOCKED` before large build steps run.

#### Scenario: Two-node E2E artifacts
- **WHEN** a two-node E2E run is executed
- **THEN** evidence uses `artifacts/two-node-e2e/<run_id>/` or an explicitly configured `/scratch/frd_muziyao/...` root
- **AND** evidence separates compute control, display service, cross-plane, manual ops boundary, Docker, DB, API, browser, Slurm, and logs.

### Requirement: Cross-plane pass gates

The two-node Docker E2E SHALL only pass when display readonly boundaries and strict run identity are proven.

#### Scenario: Display security gate
- **WHEN** the 27 Docker display service is validated
- **THEN** the evidence proves no Slurm route, no retry/cancel execution, no Slurm/Munge/Docker socket capability, readonly DB use, and readonly published artifact access
- **AND** failure of any boundary marks display service E2E as fail.

#### Scenario: Cross-plane run identity gate
- **WHEN** cross-plane E2E is evaluated
- **THEN** 27 latest-product, `/hydro-met`, `/ops`, and job logs must point to the same `run_id/source/cycle_time/model_id` produced by 22
- **AND** historical latest or mocked API data cannot satisfy the pass condition.

#### Scenario: GFS and IFS pass scope
- **WHEN** the two-node E2E plan includes both GFS and IFS for a run
- **THEN** both sources must pass strict identity latest-product, series, ops, logs, and browser source-switch checks before cross-plane status is `PASS`
- **AND** a single-source run is reported as reduced scope or `PARTIAL`, not full cross-plane `PASS`.

#### Scenario: Manual ops boundary gate
- **WHEN** retry/cancel behavior is validated
- **THEN** 27 proves fail-closed read-only behavior and 22 proves any actual retry/cancel receipt
- **AND** 27 only displays the resulting state and logs after 22 acts.
