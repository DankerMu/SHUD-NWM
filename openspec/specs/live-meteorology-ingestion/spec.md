# live-meteorology-ingestion Specification

## Purpose

How live meteorological cycles are ingested with their provenance intact: raw download lineage, source lineage carried through canonical and forcing products, and a raw-retention unit that can start on a fresh host.

## Requirements

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

### Requirement: Live cycle download records raw lineage

The system SHALL discover and download at least one available live or production-like GFS/IFS/ERA5 cycle with manifest and QC evidence.

#### Scenario: Live source cycle is downloaded and checked

- **WHEN** a configured source cycle is available
- **THEN** raw files are downloaded or confirmed present under the object-store raw prefix
- **AND** file count, size, checksum, retry count, source URL identity, cycle time, and status are recorded
- **AND** incomplete or unavailable cycles fail with stable source-status evidence rather than silent success

#### Scenario: Source download evidence is bounded and redacted

- **WHEN** raw source files are enumerated, downloaded, or verified
- **THEN** manifest enumeration, per-file reads/downloads, retry/backoff, network timeouts, forecast-hour counts, and evidence payload sizes are bounded
- **AND** source URLs, object prefixes, stdout, and evidence redact credential-shaped values

### Requirement: Canonical and forcing products keep source lineage

The system SHALL convert live raw data into canonical products and model forcing with traceable source/QC metadata.

#### Scenario: Live cycle becomes forcing for a Basins-backed model

- **WHEN** raw live source data is converted and forcing is produced for a Basins-backed model
- **THEN** canonical product metadata records variables, units, time axis, source cycle, and object URI
- **AND** forcing QC records continuity, missing values, variable ranges, and pass/fail status
- **AND** best-available lineage records selected source per valid time or an explicit skipped/restricted reason
