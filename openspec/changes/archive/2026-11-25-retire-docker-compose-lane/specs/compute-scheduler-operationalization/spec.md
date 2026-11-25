## MODIFIED Requirements

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

## REMOVED Requirements

### Requirement: Docker scheduler commands are business runnable

**Reason**: The requirement is stated for the compute Docker deployment and its first scenario runs the compose `scheduler-once` service; both are retired with the docker/compose lane (#3231).

**Migration**: Replaced by "The scheduler timer runs bounded passes", which keeps the two scenarios that describe the live `nhms-compute-scheduler.timer`.

### Requirement: Node-22 environment-file authority is documented

**Reason**: Two scenarios are about `infra/env/compute.example` and its host instance `compute.env`; the template is deleted with the docker/compose lane (#3231).

**Migration**: Replaced by "The compute units' environment-file authority is documented", which keeps the unit-to-file table and the reader scenario.

## ADDED Requirements

### Requirement: The compute units' environment-file authority is documented

`infra/env/README.md` SHALL carry a table mapping every node-22 user systemd unit (`nhms-compute-scheduler`,
`nhms-scheduler-evidence-retention`, `nhms-scheduler-file-provider-refresh`, `nhms-compute-api`, `nhms-slurm-gateway`)
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
