# scheduler-allowed-cycles Specification

## Purpose
TBD - created by archiving change issue-495-scheduler-allowed-cycles. Update Purpose after archive.
## Requirements
### Requirement: Scheduler Allowed Cycle Hours

The production scheduler SHALL support a configured allowlist of UTC cycle
hours for business candidate selection.

#### Scenario: Production config allows only 00 and 12

- **GIVEN** `NHMS_SCHEDULER_ALLOWED_CYCLE_HOURS_UTC=0,12`
- **WHEN** `ProductionSchedulerConfig` is constructed
- **THEN** the scheduler stores the sorted allowed hours `[0, 12]`
- **AND** runtime config evidence includes `allowed_cycle_hours_utc: [0, 12]`

#### Scenario: Invalid allowed cycle config fails closed

- **GIVEN** `NHMS_SCHEDULER_ALLOWED_CYCLE_HOURS_UTC` is configured as empty,
  non-integer, or outside `0..23`
- **WHEN** scheduler config is constructed
- **THEN** construction fails before scheduler discovery or submit work starts

### Requirement: Scheduler Hard Gate Filters Disallowed Cycles

The scheduler SHALL reject discovered cycles whose UTC hour is not in the
configured allowlist before candidate selection, backfill gap accounting,
readiness checks, forcing production, or submit side effects.

#### Scenario: Adapter still returns four cycles

- **GIVEN** allowed hours are `[0, 12]`
- **AND** a source discovery window returns cycles at `00`, `06`, `12`, and
  `18`
- **WHEN** the scheduler discovers production cycles
- **THEN** only `00` and `12` cycles may enter candidates
- **AND** `06` and `18` cycles do not enter `candidates` or
  `blocked_candidates`
- **AND** `06` and `18` cycles are not counted in backfill gap totals

#### Scenario: Disallowed cycles cannot affect dedupe or completion lookup

- **GIVEN** allowed hours are `[0, 12]`
- **AND** source discovery returns allowed and disallowed cycles in the same
  window
- **WHEN** the scheduler dedupes and evaluates completion status
- **THEN** disallowed `06` and `18` cycles do not consume dedupe keys or replace
  allowed cycles
- **AND** completion status lookup is not called for disallowed cycles
- **AND** latest or oldest allowed-cycle selection is unchanged by the presence
  of disallowed cycles

#### Scenario: Disallowed cycles remain auditable

- **GIVEN** allowed hours are `[0, 12]`
- **AND** source discovery returns a `06` or `18` cycle
- **WHEN** scheduler evidence is emitted
- **THEN** the disallowed cycle evidence includes
  `selection_status=excluded`
- **AND** `selection_reason=cycle_hour_not_allowed`

#### Scenario: Disallowed cycles have no downstream side effects

- **GIVEN** allowed hours are `[0, 12]`
- **AND** source discovery returns only a `06` or `18` cycle that would
  otherwise look available
- **WHEN** the scheduler runs discovery and selection
- **THEN** canonical readiness is not requested for that cycle
- **AND** forcing production is not requested for that cycle
- **AND** no orchestrator submit path is invoked for that cycle

### Requirement: Cycle Boundary Flooring Uses Allowed Hours

The scheduler SHALL compute source discovery floor boundaries from configured
allowed cycle hours rather than fixed source defaults.

#### Scenario: Current time is near a disallowed cycle

- **GIVEN** allowed hours are `[0, 12]`
- **AND** the current UTC time is near `06` or `18`
- **WHEN** the scheduler floors the source cycle boundary
- **THEN** the result is the nearest prior allowed `00` or `12` boundary
- **AND** not the disallowed `06` or `18` boundary

