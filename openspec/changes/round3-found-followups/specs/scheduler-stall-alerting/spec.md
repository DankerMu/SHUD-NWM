## ADDED Requirements

### Requirement: The lock-contended verdict names the lease refusal reason

The receipt's `signals` block SHALL carry `lock_contended_reason` beside `lock_contended_streak` on every tick. Its value SHALL be the `lock.reason` recorded by the newest graded pass when that pass reports the lock-contended status, and null otherwise. The receipt SHALL clip it the same way it clips every other evidence string, and SHALL record no path. An operator can then tell an ordinary contended lease from a lease guard that fails closed on configuration, for example `unsafe_lock_guard_mode` or `retired_lock_guard_mode_key:NHMS_SCHEDULER_LEASE_GUARD_MODE`. Both shapes write the same `lock_contended` status, and their remedies differ. The reason SHALL be informational only. The verdict, its precedence, its runbook anchor, its exit code and the lock-contended streak SHALL stay exactly as before. A missing or non-string reason SHALL be recorded as null and SHALL NOT make the tick probe-failed.

#### Scenario: A config-refused lease is distinguishable from contention

- **WHEN** the configured number of consecutive graded passes report the lock-contended status and the newest of them records `lock.reason` `retired_lock_guard_mode_key:NHMS_SCHEDULER_LEASE_GUARD_MODE`
- **THEN** the probe returns the lock-contended verdict with its unchanged exit code and runbook anchor
- **AND** `signals.lock_contended_reason` carries that reason string

#### Scenario: No contention records a null reason

- **WHEN** the newest graded pass does not report the lock-contended status
- **THEN** `signals.lock_contended_reason` is null and every other receipt field is unchanged

#### Scenario: An absent reason does not degrade the tick

- **WHEN** the lock-contended verdict is returned and the newest lock-contended pass carries no `lock.reason`, or carries one that is not a string
- **THEN** `signals.lock_contended_reason` is null and the verdict is unchanged
