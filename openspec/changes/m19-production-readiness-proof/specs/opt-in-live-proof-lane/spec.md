## ADDED Requirements

### Requirement: Opt-in Live Proof Lane
The readiness framework SHALL support live proof ingestion/execution only when explicitly configured.

#### Scenario: Live auth proof configured
WHEN live auth configuration is provided
THEN the lane records provider metadata, role mapping, protected action checks, and redacted evidence.

#### Scenario: Live alert sink configured
WHEN an alert sink is configured
THEN the lane records delivery result or stable failure without leaking sink credentials.

#### Scenario: Live rollback drill configured
WHEN live rollback execution is explicitly requested
THEN the lane records preconditions, command, result, residual risk, and artifact references.
