# facade-shrink Specification

## Purpose
Let scheduler preflight live outside `services/orchestrator/scheduler.py` without changing its behaviour or the names callers reach through the scheduler facade.

## Requirements

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
