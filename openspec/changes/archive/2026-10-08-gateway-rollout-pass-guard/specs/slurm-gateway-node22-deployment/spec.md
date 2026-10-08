## ADDED Requirements

### Requirement: Gateway rollout and rollback refuse while a scheduler pass is running

The documented gateway rollout and rollback procedures SHALL decide whether a scheduler pass is running from the state printed by `systemctl --user is-active nhms-compute-scheduler.service`, SHALL stop before any probe start, backup, overwrite or restore when that state is `active`, `activating`, `deactivating` or `reloading` or cannot be read, and SHALL NOT wait, retry, stop or kill the service.

#### Scenario: A pass is running
- **WHEN** the scheduler service is `activating` at step 0 of the rollout or the rollback
- **THEN** the step prints that a pass is still running and how to re-run, and exits 1 before any backup or restore

#### Scenario: The state cannot be read
- **WHEN** `is-active` prints nothing or an unknown word
- **THEN** the step names what was printed and exits 1

#### Scenario: No pass is running
- **WHEN** the scheduler service is `inactive`
- **THEN** the step continues to the fence probes

#### Scenario: The last pass failed
- **WHEN** the scheduler service is `failed`
- **THEN** the step prints that the fence probe requires `inactive` and how to reset the unit, and continues without resetting it
