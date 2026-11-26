# compute-scheduler-operationalization Specification

## Purpose
Specify how the node-22 compute scheduler is run in production: the runtime environment defaults it honors, the
business-runnable scheduler commands, the end-to-end automation evidence it must produce, which environment files
and tracked templates are authoritative for each node-22 unit (including the Slurm private-lane keys a rebuilt env
must carry), and how the exact-cycle wrapper serializes with the scheduler timer.

## Requirements

### Requirement: Scheduler honors runtime environment defaults

The production scheduler CLI SHALL use configured runtime roots and evidence paths when explicit CLI flags are absent.

#### Scenario: Workspace root from environment

- **WHEN** `WORKSPACE_ROOT` is set and `nhms-pipeline plan-production` is run without `--workspace-root`
- **THEN** the scheduler uses `WORKSPACE_ROOT`
- **AND** it does not default to `.nhms-workspace` inside the application directory.

#### Scenario: Canonical runtime roots from environment

- **WHEN** scheduler runs from its systemd unit without explicit root flags
- **THEN** it resolves `WORKSPACE_ROOT`, `OBJECT_STORE_ROOT`, `NHMS_PUBLISHED_ARTIFACT_ROOT`, scheduler lock root, and scheduler evidence root from documented environment variables or config defaults
- **AND** those resolved roots are included in redacted scheduler evidence.

#### Scenario: Evidence and lock roots are configured

- **WHEN** scheduler runs from its systemd unit
- **THEN** scheduler lock and evidence roots are written under `WORKSPACE_ROOT`
- **AND** object-store, published-artifact, runtime, and temporary roots are within the independently configured approved roots
- **AND** the command does not write large temporary products to the system disk by default.

#### Scenario: Published root required before mutation

- **WHEN** scheduler is configured to produce node-27-readable products
- **THEN** it verifies `NHMS_PUBLISHED_ARTIFACT_ROOT` or the configured published root is present and writable before submitting download, Slurm, SHUD, parse, or publish mutation
- **AND** an unavailable published root is recorded as a pre-mutation blocker.

#### Scenario: Invalid runtime roots block before mutation

- **WHEN** workspace, object-store, published, lock, or evidence roots are missing or unwritable
- **THEN** scheduler preflight reports a blocker before download, Slurm, SHUD, hydro, or publish mutation.

### Requirement: Node-22 end-to-end automation evidence
The system SHALL provide automated tests or commands that prove node-22 QHH business flow from fresh forecast to published results.

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
- **AND** final node-22 business automation readiness remains false until accepted live receipts exist.

### Requirement: Node-22 exact-cycle wrapper serializes with the scheduler timer

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

### Requirement: The db-free scheduler env template SHALL carry every maintained Slurm private-lane key

`infra/env/compute.scheduler-dbfree.env.example` SHALL set each Slurm private-lane key that names a maintained private
copy and whose absence makes the Slurm job env fall back to a shared key: `NHMS_SLURM_SCHEDULER_STATE_INDEX` and
`NHMS_SLURM_SCHEDULER_REGISTRY_MANIFEST`. The `nhms-required-keys: compute.scheduler-dbfree` block in
`infra/env/README.md` SHALL list both, so that the template test fails when the template loses one. A private-lane key
with no producer SHALL NOT be added to the template: `NHMS_SLURM_SCHEDULER_CANONICAL_READINESS_INDEX` stays unset, so
jobs fall back to the shared canonical readiness index that provider refresh maintains, and the template and the README
SHALL say so.

#### Scenario: An env rebuilt from the template keeps the maintained private lanes

- **WHEN** `compute.scheduler-dbfree.env` is rebuilt from the tracked template
- **THEN** `NHMS_SLURM_SCHEDULER_STATE_INDEX` and `NHMS_SLURM_SCHEDULER_REGISTRY_MANIFEST` are present
- **AND** `NHMS_SLURM_SCHEDULER_CANONICAL_READINESS_INDEX` is absent, and the template comment names the shared index
  as the value jobs receive

#### Scenario: Losing a maintained private-lane key fails the template test

- **WHEN** `NHMS_SLURM_SCHEDULER_STATE_INDEX` or `NHMS_SLURM_SCHEDULER_REGISTRY_MANIFEST` is removed from
  `compute.scheduler-dbfree.env.example`
- **THEN** `tests/test_env_templates.py` fails on the README required-keys block

### Requirement: The compute units' environment-file authority is documented

`infra/env/README.md` SHALL carry a table mapping every node-22 user systemd unit (`nhms-compute-scheduler`,
`nhms-scheduler-evidence-retention`, `nhms-scheduler-file-provider-refresh`, `nhms-slurm-gateway`)
to the EnvironmentFile(s) it loads and to the tracked template for each, marking `compute.host.env` as untracked with
no template, and SHALL state that an `infra/env/compute.env` still present on the host is a leftover of the retired
docker/compose lane that no unit reads and that has no tracked template.

#### Scenario: Reader resolves the scheduler's basin configuration

- **WHEN** a reader wants the production scheduler's `NHMS_BASINS_ROOT` or `NHMS_SCHEDULER_BASIN_IDS`
- **THEN** `infra/env/README.md` directs them to `compute.scheduler-dbfree.env` (template `compute.scheduler-dbfree.env.example`)
- **AND** states that the values of a leftover `compute.env` are not the scheduler's configuration

### Requirement: The scheduler timer runs bounded passes

The compute deployment SHALL run the scheduler from a systemd timer whose passes can run without manual flag patching.

#### Scenario: Continuous or timer mode is bounded

- **WHEN** continuous scheduler mode or a systemd timer is enabled
- **THEN** it uses a lock/lease to prevent duplicate passes
- **AND** interval, max cycles, source filters, retry policy, and evidence root are explicit in config or docs.

#### Scenario: Business loop can be disabled safely

- **WHEN** an operator disables the scheduler timer
- **THEN** active run evidence remains queryable
- **AND** no new work is submitted after the current bounded pass exits.
