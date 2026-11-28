## RENAMED Requirements

- FROM: `### Requirement: Node-22 end-to-end automation evidence`
- TO: `### Requirement: End-to-end automation evidence of the compute role`
- FROM: `### Requirement: Node-22 exact-cycle wrapper serializes with the scheduler timer`
- TO: `### Requirement: The exact-cycle wrapper serializes with the scheduler timer`

## MODIFIED Requirements

### Requirement: End-to-end automation evidence of the compute role
The system SHALL provide automated tests or commands that prove the compute role's QHH business flow from fresh forecast to published results.

#### Scenario: Live E2E pass
- **WHEN** live forecast source, QHH bootstrap, real SHUD, Slurm, DB, workspace, and published root are all available
- **THEN** the E2E command completes download, canonical conversion, forcing, SHUD Slurm execution, parse, publish, and pipeline evidence for one QHH cycle
- **AND** it reports run identity, stage statuses, DB row counts, artifact paths, Slurm receipts, and published manifest/log URIs.

#### Scenario: Live dependency blocked
- **WHEN** one required live dependency is unavailable
- **THEN** the E2E command reports `BLOCKED` with the exact dependency and evidence path
- **AND** it does not mark business automation or production readiness as passed.

#### Scenario: Deterministic tests cannot claim live readiness
- **WHEN** deterministic fixtures or mocked gateways are used in CI
- **THEN** tests can validate contracts and state transitions
- **AND** final business automation readiness of the compute role remains false until accepted live receipts exist.

### Requirement: The exact-cycle wrapper serializes with the scheduler timer

`scripts/ops/node22-run-cycle-once.sh` SHALL run with the same environment the `nhms-compute-scheduler.service` unit loads: the scheduler env file, the Slurm gateway secret env file (default path identical to the tracked drop-in), and `NHMS_PYTHON_VENV_BIN`.

- In `--submit` mode it SHALL refuse to run (exit 2) when the secret file is missing or unreadable. In `--plan` mode it SHALL only warn.
- When `nhms-compute-scheduler.timer` is active, it SHALL stop the timer and wait, with a bounded timeout, until the service is no longer running before invoking the planner.
- It SHALL restart a timer it stopped on every exit path: success, planner failure, wait timeout, INT, TERM, HUP, or QUIT. On such a signal it SHALL forward TERM to the planner process (INT and QUIT cannot reach a background child of a non-interactive shell) and wait for it before exiting. A broken stderr pipe (SIGPIPE) SHALL NOT terminate it. It SHALL NOT start a timer that was inactive on entry.
- It SHALL fail closed (exit 2) when the timer or service state cannot be determined, or when stopping the timer fails.
- Secret values SHALL NOT appear in its stdout or stderr. Stdout SHALL carry only planner output.

#### Scenario: Active timer is paused around the run and restored

- **WHEN** the operator runs the wrapper with `--plan` while the timer is active and a scheduler pass is still running
- **THEN** the wrapper stops the timer, waits until the service is inactive, runs the planner, and starts the timer again before exiting with the planner's exit code

#### Scenario: Wait timeout restores the timer without running the planner

- **WHEN** the scheduler service is still running when the wait bound elapses
- **THEN** the wrapper exits non-zero with `timer_wait_timeout` on stderr, the planner is never invoked, and the timer is started again

#### Scenario: Submit without the gateway secret is refused

- **WHEN** the operator runs the wrapper with `--submit` and the gateway secret env file does not exist
- **THEN** the wrapper exits 2 without touching the timer and without invoking the planner

### Requirement: The compute units' environment-file authority is documented

`infra/env/README.md` SHALL carry a table mapping every user systemd unit of the compute role (`nhms-compute-scheduler`,
`nhms-scheduler-evidence-retention`, `nhms-scheduler-file-provider-refresh`, `nhms-slurm-gateway`)
to the EnvironmentFile(s) it loads and to the tracked template for each, marking `compute.host.env` as untracked with
no template, and SHALL state that an `infra/env/compute.env` still present on the host is a leftover of the retired
docker/compose lane that no unit reads and that has no tracked template.

#### Scenario: Reader resolves the scheduler's basin configuration

- **WHEN** a reader wants the production scheduler's `NHMS_BASINS_ROOT` or `NHMS_SCHEDULER_BASIN_IDS`
- **THEN** `infra/env/README.md` directs them to `compute.scheduler-dbfree.env` (template `compute.scheduler-dbfree.env.example`)
- **AND** states that the values of a leftover `compute.env` are not the scheduler's configuration
