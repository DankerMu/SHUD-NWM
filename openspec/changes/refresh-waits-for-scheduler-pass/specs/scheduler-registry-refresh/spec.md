## ADDED Requirements

### Requirement: The provider refresh waits for a running scheduler pass

The refresh wrapper SHALL read the state of `nhms-compute-scheduler.service` from the output of `systemctl --user is-active`, SHALL treat `active`, `activating`, `deactivating` and `reloading` as a running pass and wait for it to end before refreshing, SHALL proceed on `inactive` or `failed`, and SHALL exit non-zero without refreshing when the state is anything else or when the pass is still running after the wait bound. The refresh unit SHALL keep `Before=nhms-compute-scheduler.service` and SHALL NOT carry a start condition on the scheduler service.

#### Scenario: A pass is running when the refresh starts
- **WHEN** the scheduler service is `activating` at the first query and `inactive` at a later one
- **THEN** the wrapper logs that it waits, runs the refresh after the later query, and exits with the refresh's status

#### Scenario: No pass is running
- **WHEN** the scheduler service is `inactive` or `failed`
- **THEN** the refresh runs at once

#### Scenario: The state cannot be read
- **WHEN** `systemctl` is missing, prints nothing or prints an unknown word
- **THEN** the wrapper exits 3, names what was printed, and the refresh does not run

#### Scenario: The pass outlasts the bound
- **WHEN** the scheduler service is still running after the wait bound
- **THEN** the wrapper exits 3, names the state and the time waited, and the refresh does not run
