## ADDED Requirements

### Requirement: State snapshot index capacity SHALL be watched daily with a failing unit as the alert

node-22 SHALL run a daily user-level oneshot unit that performs the `prune-retention` dry-run of both state index lanes. The run SHALL never pass `enforce`, and SHALL never write either index, the repair archive root or the repair receipt root. It SHALL write a bounded, owner-private receipt that records, for each lane:

- `checksum_valid`
- `entry_count_before`
- `removed_count`
- `removed_state_ids_sha256`
- the full `capacity_before` and `capacity_after` objects

The receipt SHALL exclude the removed id list and the group summaries.

The unit SHALL fail (exit non-zero) when any of the following holds:

- either lane reports `capacity.warning=true`;
- either lane reports `checksum_valid=false`;
- either lane would still carry a capacity warning after pruning;
- either lane reports zero entries;
- a lane summary is missing;
- the dry-run refuses or reports an incomplete result;
- the receipt cannot be written;
- the watch itself fails unexpectedly.

Otherwise the unit SHALL exit 0. The enforce step SHALL remain the manual runbook flow that this alert triggers.

#### Scenario: A healthy index passes and leaves a receipt

- **WHEN** both lanes have a valid checksum, a non-zero entry count and no capacity warning
- **THEN** the unit exits 0
- **AND** the receipt carries both lanes' capacity and removal digest
- **AND** both index files are byte-identical before and after the run

#### Scenario: Capacity crossing the warning threshold fails the unit

- **WHEN** either lane's `capacity_before.warning` is true
- **THEN** the unit exits non-zero
- **AND** the receipt lists a `capacity_warning` alert for that lane

#### Scenario: A missing cycle lag fails closed

- **WHEN** `NHMS_SCHEDULER_CYCLE_LAG_HOURS` is not set
- **THEN** the unit exits non-zero with the `repair_cycle_lag_unset` refusal
- **AND** no index file is opened, and nothing is written outside the watch's own `refused` receipt

#### Scenario: An invalid checksum fails the unit

- **WHEN** either lane reports `checksum_valid=false`
- **THEN** the unit exits non-zero
- **AND** the receipt lists a `checksum_invalid` alert for that lane

#### Scenario: The watch is dry-run only

- **WHEN** the unit runs with the repair archive and repair receipt environment variables set
- **THEN** it never requests enforce
- **AND** no file appears under the archive root or the repair receipt root
- **AND** both index files are byte-identical before and after

#### Scenario: A warning that pruning cannot clear is reported distinctly

- **WHEN** either lane's `capacity_after.warning` is true
- **THEN** the unit exits non-zero
- **AND** the receipt lists `capacity_warning_unprunable` for that lane
