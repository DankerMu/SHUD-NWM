## MODIFIED Requirements

### Requirement: Tracked node-22 oneshot units SHALL be wired to the alert
Each of `nhms-node22-refresh-timer-health.service`, `nhms-node22-scheduler-stall-health.service`, `nhms-scheduler-evidence-retention.service`, `nhms-scheduler-file-provider-refresh.service`, `nhms-scheduler-journal-retention.service`, `nhms-scheduler-state-index-capacity.service` and `nhms-scheduler-state-index-prune.service` under `infra/systemd/` SHALL carry exactly one `OnFailure=nhms-node22-unit-failure-alert@%n.service` line in `[Unit]`. A test SHALL assert all of the following:

- the line occurs exactly once in each of these seven files;
- it occurs zero times in `nhms-slurm-gateway.service` and in every file under `infra/systemd/node22/`;
- the `MANIFEST.json` sha256 entries for the seven were updated in the same change.

#### Scenario: Six oneshots wired, long-running units untouched
- **WHEN** the unit-wiring test scans `infra/systemd/` (scenario title kept for archive continuity; since #3121 the set is seven)
- **THEN** each of the seven named oneshot units contains the `OnFailure=` line once
- **AND** the gateway unit and `node22/**` contain none
- **AND** the manifest sha256 of the seven matches their new content
