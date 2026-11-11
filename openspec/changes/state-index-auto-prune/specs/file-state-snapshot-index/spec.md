## ADDED Requirements

### Requirement: State index prune-retention SHALL be enforced automatically at the scheduler pass boundary

node-22 SHALL run a daily user-level oneshot unit that performs a `prune-retention` dry-run. When either lane's `capacity_before.warning` is true, it SHALL enforce the prune. The window SHALL be `max(DEFAULT_STATE_INDEX_RETENTION_DAYS, minimum_days)`, computed from the scheduler cycle lag through the same floor helper the repair uses.

Before enforcing, the unit SHALL do all of the following:

- Refuse to touch any timer when a `node22-run-cycle-once.sh` wrapper is running.
- Stop `nhms-compute-scheduler.timer` and `nhms-scheduler-file-provider-refresh.timer`, but only those that were active.
- Wait, with a bounded wait, until both services are idle.
- Re-check the wrapper and both services.
- Require a final dry-run in which both lanes have a valid checksum and a non-zero entry count, and every lane over the warning threshold has a non-zero removal count. The same gate SHALL first be evaluated on the initial dry-run, before any timer is touched.

Every timer the unit stopped SHALL be started again exactly once before exit, whether the outcome is success, refusal, timeout, an error, or SIGTERM/SIGINT/SIGHUP. A timer the unit did not stop SHALL NOT be started.

The unit SHALL exit non-zero, without retrying, when any of the following holds:

- a wrapper is running;
- the gate fails;
- the wait times out;
- a service state cannot be determined;
- the enforce is incomplete;
- capacity is still at or above the warning threshold after pruning;
- any timer cannot be restored.

#### Scenario: Below threshold does nothing

- **WHEN** neither lane has `capacity_before.warning`
- **THEN** no timer is stopped, no enforce runs, and the unit exits 0 with `action=skip`

#### Scenario: Over threshold prunes at the pass boundary and restores the timers

- **WHEN** a lane is over the threshold, no wrapper is running, and the scheduler pass then finishes
- **THEN** the timers are stopped, the services are idle, the gate passes and the enforce runs
- **AND** both timers are active again when the unit exits 0 with `action=enforced`

#### Scenario: Any failure after freezing still restores the timers

- **WHEN** the wait times out, the gate fails, the enforce is incomplete or errors, or the unit receives SIGTERM
- **THEN** every timer the unit stopped is started again
- **AND** the unit exits non-zero

## MODIFIED Requirements

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

Otherwise the unit SHALL exit 0. The watch itself SHALL stay dry-run only. Since #3121 the enforce step SHALL be performed by the daily auto-prune unit (see "State index prune-retention SHALL be enforced automatically at the scheduler pass boundary"), and the manual runbook flow SHALL remain the fallback.

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
