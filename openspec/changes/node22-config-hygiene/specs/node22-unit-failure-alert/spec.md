# Spec Delta

## Purpose
A failed node-22 oneshot unit becomes a mail alert through a generic `OnFailure=` template, so timer-driven probes and retention lanes cannot fail silently.

## ADDED Requirements

### Requirement: A generic node-22 unit-failure alert template SHALL exist
The repository SHALL track `infra/systemd/nhms-node22-unit-failure-alert@.service` (user scope, `Type=oneshot`, `WorkingDirectory=/scratch/frd_muziyao/NWM`, `EnvironmentFile=-%h/NWM/infra/env/node22-unit-failure-alert.env`, `ExecStart=/scratch/frd_muziyao/NWM/scripts/node22_unit_failure_alert_once.sh %i`, `TimeoutStartSec=120`) and the handler `scripts/node22_unit_failure_alert_once.sh`, which reads at most 30 journal lines of the failed unit named by `%i`, composes one message and hands it to `$NHMS_FRONTIER_SENDMAIL`. With no configured recipient the handler SHALL exit 0 after logging that delivery was skipped. The tracked example `infra/env/node22-unit-failure-alert.example` SHALL list the same variable names as the node-27 frontier-alert lane.

#### Scenario: Failed unit produces one mail
- **WHEN** the handler runs with `%i` set to a failed unit name and a configured recipient and sendmail shim
- **THEN** exactly one message naming the unit and containing its last journal lines is handed to the shim and the handler exits 0

#### Scenario: Unconfigured recipient does not add a second failure
- **WHEN** the handler runs without `NHMS_ALERT_EMAIL_TO`
- **THEN** it logs that delivery was skipped and exits 0

### Requirement: Tracked node-22 oneshot units SHALL be wired to the alert
Each of `nhms-node22-refresh-timer-health.service`, `nhms-node22-scheduler-stall-health.service`, `nhms-scheduler-evidence-retention.service`, `nhms-scheduler-file-provider-refresh.service`, `nhms-scheduler-journal-retention.service` and `nhms-scheduler-state-index-capacity.service` under `infra/systemd/` SHALL carry exactly one `OnFailure=nhms-node22-unit-failure-alert@%n.service` line in its `[Unit]` section. A test SHALL assert the line is present on exactly these six units and absent from the scheduler, gateway and API units.

#### Scenario: Six oneshots wired, long-running units untouched
- **WHEN** the unit-wiring test scans `infra/systemd/`
- **THEN** the six named oneshot units each contain the `OnFailure=` line once
- **AND** `nhms-slurm-gateway.service` and every file under `infra/systemd/node22/` contain no `OnFailure=` line
