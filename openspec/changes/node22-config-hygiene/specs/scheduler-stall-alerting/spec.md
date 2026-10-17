# Spec Delta

## ADDED Requirements

### Requirement: A preflight-blocked scheduler SHALL become a non-zero unit
The stall probe SHALL read `execution_boundary` from each pass artifact. A pass whose `execution_boundary` ends in `preflight_blocked` SHALL be classified as the `root-blocked` shape regardless of its counts. When the newest pass is `root-blocked` and the time span from the oldest consecutive `root-blocked` pass to the newest exceeds `NHMS_SCHEDULER_STALL_ROOT_BLOCKED_MINUTES` (default 60), the verdict SHALL be `scheduler_root_blocked`, exit status 1, runbook section `stall-scheduler-root-blocked`. This verdict SHALL rank below `probe_failed`, `timer_not_enabled`, `timer_stopped` and `scheduler_service_failed` and above `submission_stalled` and `no_progress`. A `root-blocked` pass SHALL neither reset nor count toward the no-submission streak.

#### Scenario: One hour of preflight-blocked passes
- **WHEN** the newest thirteen passes, spanning 65 minutes, all carry `execution_boundary: scheduler_root_preflight_blocked` with zero counts
- **THEN** the verdict is `scheduler_root_blocked`, the receipt names the runbook section and the exit status is 1

#### Scenario: A single transient refusal stays ok
- **WHEN** only the newest pass is `root-blocked` and the pass before it submitted normally within the window
- **THEN** the verdict is `ok`

#### Scenario: Dead lane outranks blocked lane
- **WHEN** the scheduler timer is stopped and the newest passes are `root-blocked` beyond the threshold
- **THEN** the verdict is `timer_stopped`

#### Scenario: Blocked passes do not reset the no-submission streak
- **WHEN** a `root-blocked` pass sits between two passes that count toward the no-submission streak
- **THEN** the streak computed across them is the same as if the `root-blocked` pass were absent
