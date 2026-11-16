# Spec Delta: live-meteorology-ingestion

## ADDED Requirements

### Requirement: Raw-retention systemd unit MUST bootstrap its log directory before ExecStart

The `nhms-node27-raw-retention.service` user-level systemd unit SHALL create its
own log directory in the `[Service]` block before `ExecStart` runs, so that a
clean-node bringup (DR restore, migration, or second-cluster deployment) does
not fail with `Failed to open standard output` on the first `systemctl --user
start`.

#### Scenario: ExecStartPre creates the log directory

- **WHEN** the `[Service]` block declares
  `StandardOutput=append:/home/nwm/node27-raw-retention-logs/systemd.log` and
  the equivalent `StandardError`
- **THEN** the same block SHALL declare
  `ExecStartPre=/usr/bin/mkdir -p /home/nwm/node27-raw-retention-logs`
- **AND** the `ExecStartPre` directive SHALL appear before `ExecStart` in the
  unit file so systemd runs the directory bootstrap before it opens the
  append-mode log file descriptors

#### Scenario: Regression test enforces the invariant

- **WHEN** an automated test reads
  `infra/systemd/nhms-node27-raw-retention.service`
- **THEN** the test SHALL assert the `ExecStartPre=/usr/bin/mkdir -p
  /home/nwm/node27-raw-retention-logs` substring, the
  `StandardOutput=append:/home/nwm/node27-raw-retention-logs/systemd.log`
  substring, and that the `ExecStartPre` line index is strictly less than the
  `ExecStart` line index

#### Scenario: Operator install-guide lists the log directory

- **WHEN** an operator follows the runbook
  `docs/runbooks/tier-node27-timeseries-storage.md` §Install step 1
- **THEN** the pre-install `mkdir -p` line SHALL include
  `~/node27-raw-retention-logs` so an operator without the tribal-knowledge
  step from the original #849 deployment window can bring up a clean node
  without the raw-retention unit failing on first start
