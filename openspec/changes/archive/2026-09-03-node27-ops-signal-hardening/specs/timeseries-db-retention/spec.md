## ADDED Requirements

### Requirement: The §8.6 escalation criterion MUST count drop-phase refusals regardless of SQLSTATE

The runbook criterion that decides whether retention has stopped progressing SHALL count refused ticks whose `refusal_reason` carries the `RETENTION_DROP_FAILED:` prefix, using the runner's per-tick stderr diagnostic line as the counted anchor. The count SHALL NOT depend on the `lock-contention(` classification, and the counted line shape SHALL be pinned by a regression test against the runbook command.

#### Scenario: A statement-timeout refusal is counted

- **WHEN** a retention tick refuses because `drop_chunks` raised SQLSTATE `57014`
- **THEN** exactly one diagnostic line carrying `RETENTION_DROP_FAILED:` is emitted for that tick
- **AND** the runbook command counts that tick

#### Scenario: Lock-classified refusals are counted the same way

- **WHEN** a tick refuses with SQLSTATE `55P03` or `40P01`
- **THEN** the same anchor is emitted exactly once and the count increments by one

#### Scenario: A clean tick is not counted

- **WHEN** a tick completes without a drop failure
- **THEN** no counted anchor is emitted

### Requirement: The retention runner's stderr MUST reach the journal while retention.log stays complete

The retention wrapper SHALL deliver the runner's combined output both to `retention.log` and to its own stderr, the unit SHALL route stderr to the journal, and the wrapper's exit code SHALL equal the runner's exit code. The refusal diagnostic in the journal SHALL be the redacted text already produced by the runner.

#### Scenario: A refused tick's reason appears in the alert context

- **WHEN** the runner exits non-zero with a `RETENTION_*` refusal
- **THEN** `journalctl --user -u nhms-node27-timeseries-retention.service -n 30` contains the diagnostic line
- **AND** the `OnFailure=` handler's mail body contains the `RETENTION_*` code without any credential material

#### Scenario: Exit code and log brackets survive the pipeline

- **WHEN** the runner exits with status 3
- **THEN** the wrapper exits with status 3
- **AND** `retention.log` contains the `start` and `done rc=3` bracket lines around the runner output

### Requirement: The resource-governance audit MUST exit non-zero on a critical recommendation

The node-27 resource-governance audit SHALL write its receipt unchanged and then exit non-zero when any recommendation has severity `critical`, printing one `RESOURCE_GOVERNANCE_CRITICAL:<code>` line per critical recommendation to stderr; its unit SHALL declare `OnFailure=nhms-node27-unit-failure-alert@%n.service` and route stderr to the journal; its lock file SHALL NOT live on the root volume.

#### Scenario: Root volume below the critical threshold

- **WHEN** root free bytes are below `root_free_critical_bytes`
- **THEN** the receipt is written with `status: completed`, stderr carries `RESOURCE_GOVERNANCE_CRITICAL:` with the recommendation code, and the process exits 1

#### Scenario: No critical recommendation

- **WHEN** every recommendation is below critical
- **THEN** the process exits 0 and no `RESOURCE_GOVERNANCE_CRITICAL:` line is printed
