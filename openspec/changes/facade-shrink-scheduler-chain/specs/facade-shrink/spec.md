## ADDED Requirements

### Requirement: Scheduler preflight implementation owner

The system SHALL keep scheduler preflight behavior stable while allowing the
implementation to live outside `services/orchestrator/scheduler.py`.

#### Scenario: Legacy scheduler preflight private names remain compatible

- **GIVEN** existing callers import scheduler preflight helpers from
  `services.orchestrator.scheduler`
- **WHEN** the implementation is moved to an owner module
- **THEN** the old private names remain importable from the facade
- **AND** focused scheduler preflight tests pass without blocker/check shape
  drift.

### Requirement: Compatibility guard remains authoritative

The system SHALL require inventory coverage for every retained scheduler or
chain facade alias introduced during facade shrink work.

#### Scenario: New facade alias has inventory coverage

- **GIVEN** a facade private name forwards to a new owner module
- **WHEN** the entropy compatibility-facade guard scans the repository
- **THEN** the matching compatibility inventory records the owner, retention
  reason, removal condition, and verification command
- **AND** the guard reports zero signals.
