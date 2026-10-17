# Spec Delta

## Purpose
A failed node-22 oneshot unit becomes a mail alert through a generic `OnFailure=` template with node-22 absolute paths, so timer-driven probes and retention lanes cannot fail silently and a broken mail channel is itself visible.

## ADDED Requirements

### Requirement: A generic node-22 unit-failure alert template SHALL exist with absolute paths
The repository SHALL track `infra/systemd/nhms-node22-unit-failure-alert@.service` (user scope, `Type=oneshot`, `WorkingDirectory=/scratch/frd_muziyao/NWM`, `EnvironmentFile=-/scratch/frd_muziyao/NWM/infra/env/node22-unit-failure-alert.env`, `ExecStart=/scratch/frd_muziyao/NWM/scripts/node22_unit_failure_alert_once.sh %i`, `TimeoutStartSec=120`) and the handler `scripts/node22_unit_failure_alert_once.sh`, which reads at most 30 journal lines of the failed unit named by `%i`, composes one message and hands it to `$NHMS_FRONTIER_SENDMAIL`. With no `NHMS_ALERT_EMAIL_TO` the handler SHALL log `SKIPPED` and exit 0; when the transport is configured and returns non-zero the handler SHALL exit non-zero. The tracked example `infra/env/node22-unit-failure-alert.example` SHALL define exactly `NHMS_ALERT_EMAIL_TO`, `NHMS_ALERT_EMAIL_FROM`, `NHMS_FRONTIER_SENDMAIL` (example value `/usr/sbin/sendmail`), `NHMS_SMTP_HOST`, `NHMS_SMTP_PORT`, `NHMS_SMTP_USER`, `NHMS_SMTP_PASS` and no other key; `infra/env/README.md` authority table SHALL gain a row for it, and `infra/systemd/node22/MANIFEST.json` SHALL gain the template's entry so the drift probe does not report it as `untracked`. No node-22 unit or template SHALL contain the literal `%h/NWM`.

#### Scenario: Failed unit produces one mail
- **WHEN** the handler runs with `%i` set to a failed unit name, `NHMS_ALERT_EMAIL_TO` set and a fake transport that exits 0
- **THEN** exactly one message naming the unit and containing its last journal lines is handed to the transport and the handler exits 0

#### Scenario: Unconfigured recipient does not add a second failure
- **WHEN** the handler runs without `NHMS_ALERT_EMAIL_TO`
- **THEN** it logs `SKIPPED` and exits 0 without invoking the transport

#### Scenario: Failing transport is visible
- **WHEN** the transport exits 1
- **THEN** the handler exits non-zero

#### Scenario: Example key set is exact
- **WHEN** the test reads `infra/env/node22-unit-failure-alert.example`
- **THEN** the set of defined keys equals the seven named keys, `DATABASE_URL` is absent, and the manifest lists the template with its sha256

### Requirement: Tracked node-22 oneshot units SHALL be wired to the alert
Each of `nhms-node22-refresh-timer-health.service`, `nhms-node22-scheduler-stall-health.service`, `nhms-scheduler-evidence-retention.service`, `nhms-scheduler-file-provider-refresh.service`, `nhms-scheduler-journal-retention.service` and `nhms-scheduler-state-index-capacity.service` under `infra/systemd/` SHALL carry exactly one `OnFailure=nhms-node22-unit-failure-alert@%n.service` line in `[Unit]`. A test SHALL assert the line occurs exactly once in each of these six files and zero times in `nhms-slurm-gateway.service` and in every file under `infra/systemd/node22/`, and that `MANIFEST.json` sha256 entries for the six were updated in the same change.

#### Scenario: Six oneshots wired, long-running units untouched
- **WHEN** the unit-wiring test scans `infra/systemd/`
- **THEN** the six named oneshot units each contain the `OnFailure=` line once, the gateway unit and `node22/**` contain none, and the manifest sha256 of the six matches their new content
