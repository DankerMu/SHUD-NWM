## ADDED Requirements

### Requirement: Sacct byte and row bound legs prove saturation, not a wall-clock timeout

The real-process sacct bound test SHALL assert, for its byte and row legs, that the query failed because the bounded output was saturated for that boundary. It SHALL NOT accept a wall-clock timeout for those legs. Their wall deadline SHALL serve only as a safety net that saturation does not reach.

#### Scenario: CPU starvation does not turn a timeout into a pass or a spurious failure

- **WHEN** the byte or row leg runs under heavy CPU load
- **THEN** it passes only if a saturation of that boundary was recorded, and the trap marker exists

#### Scenario: A forced timeout fails the leg

- **WHEN** the fake sacct times out before it produces any output
- **THEN** the byte or row leg fails on the saturation-reason assertion
