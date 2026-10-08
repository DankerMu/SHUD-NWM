## ADDED Requirements

### Requirement: The refresh start timeout covers the wait for a scheduler pass and the refresh

The refresh unit's `TimeoutStartSec` SHALL be at least the wrapper's wait bound plus 7200 seconds, the wait bound SHALL be 14400 seconds, the model succession tool's blocking start of the refresh unit SHALL be given the unit's `TimeoutStartSec` plus 300 seconds, and the timer-health probe's default stopped dwell SHALL be the unit's `TimeoutStartSec` plus 7200 seconds.

#### Scenario: A pass that outlasts the former bound
- **WHEN** the scheduler service is still running after 5400 seconds of waiting and ends before 14400 seconds
- **THEN** the wrapper runs the refresh and exits with the refresh's status

#### Scenario: The numbers stay consistent
- **WHEN** the wait bound or the unit's start timeout is edited
- **THEN** a test fails unless the unit's timeout is at least the bound plus 7200 seconds and the succession tool's start timeout is the unit's timeout plus 300 seconds and the timer-health probe's default stopped dwell is the unit's timeout plus 7200 seconds
