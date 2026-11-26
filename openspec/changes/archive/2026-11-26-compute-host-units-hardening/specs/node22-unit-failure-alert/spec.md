## MODIFIED Requirements

### Requirement: Tracked node-22 oneshot units SHALL be wired to the alert
Each of `nhms-node22-refresh-timer-health.service`, `nhms-node22-scheduler-stall-health.service`, `nhms-scheduler-evidence-retention.service`, `nhms-scheduler-file-provider-refresh.service`, `nhms-scheduler-journal-retention.service`, `nhms-scheduler-state-index-capacity.service`, `nhms-scheduler-state-index-prune.service` and `nhms-w3-journal-backup.service` under `infra/systemd/` SHALL carry exactly one `OnFailure=nhms-node22-unit-failure-alert@%n.service` line in `[Unit]`. A test SHALL assert all of the following:

- the line occurs exactly once in each of these eight files;
- it occurs zero times in `nhms-slurm-gateway.service` and in every file under `infra/systemd/node22/`;
- the `MANIFEST.json` sha256 entries for the eight were updated in the same change.

#### Scenario: Six oneshots wired, long-running units untouched
- **WHEN** the unit-wiring test scans `infra/systemd/` (scenario title kept for archive continuity; since #3359 the set is eight)
- **THEN** each of the eight named oneshot units contains the `OnFailure=` line once
- **AND** the gateway unit and `node22/**` contain none
- **AND** the manifest sha256 of the eight matches their new content

## ADDED Requirements

### Requirement: A hang of the w3 journal backup SHALL become a unit failure, and a missing source SHALL stay a skip
`infra/systemd/nhms-w3-journal-backup.service` SHALL set `TimeoutStartSec=` in `[Service]` to a whole number of seconds greater than zero and small enough that three times the value stays below the period of `nhms-w3-journal-backup.timer` (one hour): systemd arms the start timeout anew for each of the unit's three commands (`ExecCondition=`, `ExecStart=`, `ExecStartPost=`), so only that bound makes an `rsync` blocked on the `hard` `/ghdc/data` mount end as a failed unit before the next tick. The unit SHALL keep its `ExecCondition=` on the local journal directory: when that directory is absent the unit is skipped, `rsync --delete` does not run, and no alert is sent. A test SHALL assert the timeout bound and that the unit's non-comment lines are exactly the lines deployed before this change plus the `OnFailure=` line in `[Unit]` and the `TimeoutStartSec=` line in `[Service]`.

#### Scenario: The timeout is bounded below the timer period
- **WHEN** the test reads `[Service]` of `nhms-w3-journal-backup.service`
- **THEN** it finds exactly one `TimeoutStartSec=` whose value is an integer number of seconds with `0 < 3 × value < 3600`

#### Scenario: The skip condition and the copy command are unchanged
- **WHEN** the test compares the unit with the copy deployed before this change
- **THEN** the non-comment lines of each section, in order, are the earlier lines with the `OnFailure=` line added to `[Unit]` and the `TimeoutStartSec=` line added to `[Service]`, so `ExecCondition=`, `ExecStart=` and `ExecStartPost=` are byte-identical
