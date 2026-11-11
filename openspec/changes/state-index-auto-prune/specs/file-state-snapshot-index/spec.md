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
