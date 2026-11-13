## ADDED Requirements

### Requirement: The stall probe's default enumeration bound SHALL cover the evidence retention volume
`scripts/node22_scheduler_stall_health.py` SHALL default `NHMS_SCHEDULER_STALL_MAX_ENTRIES_SCANNED` to 65536, so an evidence root that holds more than 4096 entries within the 90-day evidence retention window is still graded. Reaching the configured bound SHALL remain a `probe_failed` error and SHALL NOT silently truncate the listing.

#### Scenario: Catch-up sized evidence root is graded with the default bound
- **WHEN** the evidence root holds more than 4096 entries, a healthy pass window is present, and no `NHMS_SCHEDULER_STALL_MAX_ENTRIES_SCANNED` override is set
- **THEN** the verdict is not `probe_failed` and no enumeration-bound error is reported

#### Scenario: An explicit bound below the entry count still fails closed
- **WHEN** `NHMS_SCHEDULER_STALL_MAX_ENTRIES_SCANNED` is set below the number of entries in the evidence root
- **THEN** the probe reports `probe_failed` with the enumeration-bound error
