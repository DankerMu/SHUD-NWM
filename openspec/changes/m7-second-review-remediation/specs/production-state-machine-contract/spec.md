## ADDED Requirements

### Requirement: PostgreSQL-valid retry status transitions
Manual and automatic retry operations SHALL write only statuses accepted by production PostgreSQL enum-backed tables.

#### Scenario: Manual retry updates hydro run with valid enum value
- **WHEN** an operator retries a failed run through `POST /api/v1/runs/{run_id}/retry`
- **THEN** every status written to `hydro.hydro_run.status` MUST be present in `hydro.run_status` as defined by migrations
- **AND** retry queue state MUST remain visible through `ops.pipeline_job.status` and pipeline events

#### Scenario: Retry status contract is tested against migrations
- **WHEN** tests validate retry status transitions
- **THEN** they MUST inspect or exercise the production migration enum values instead of relying only on SQLite `TEXT` columns

### Requirement: PostgreSQL-valid cancel status transitions
Cancel operations SHALL write only statuses accepted by production PostgreSQL enum-backed tables.

#### Scenario: Cancel updates forecast cycle with valid enum value
- **WHEN** an operator cancels an active run through `POST /api/v1/runs/{run_id}/cancel`
- **THEN** every status written to `met.forecast_cycle.status` or `met.forecast_cycle.current_state` MUST be present in `met.cycle_status`
- **AND** the API response MUST identify whether the cycle was changed, preserved, or not found

#### Scenario: Published or terminal cycle is preserved
- **WHEN** a cancel request targets a run whose forecast cycle is already terminal
- **THEN** the cycle status MUST be preserved
- **AND** the cancel response MUST report that preservation explicitly

### Requirement: Status schema and OpenAPI alignment
The schema and OpenAPI contracts SHALL include every externally visible status that control-plane APIs can return.

#### Scenario: New enum value is introduced
- **WHEN** a remediation adds a new database enum value such as `pending` or `cancelled`
- **THEN** it MUST add a forward-only migration
- **AND** update OpenAPI schemas, JSON schemas, frontend generated types, and status tests in the same delivery

#### Scenario: Existing enum value is reused
- **WHEN** a remediation maps retry or cancel to an existing enum value
- **THEN** OpenAPI and API response documentation MUST describe how queue/cancel detail is exposed without adding a new domain status
