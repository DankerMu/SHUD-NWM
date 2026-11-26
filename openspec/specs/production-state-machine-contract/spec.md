# production-state-machine-contract Specification

## Purpose
Keep pipeline status transitions valid against the PostgreSQL schema and aligned with the published status schema
and OpenAPI, for automatic retry.

## Requirements

### Requirement: Status schema and OpenAPI alignment
The schema and OpenAPI contracts SHALL include every externally visible status that control-plane APIs can return.

#### Scenario: New enum value is introduced
- **WHEN** a remediation adds a new database enum value such as `pending` or `cancelled`
- **THEN** it MUST add a forward-only migration
- **AND** update OpenAPI schemas, JSON schemas, frontend generated types, and status tests in the same delivery

#### Scenario: Existing enum value is reused
- **WHEN** a remediation maps retry or cancel to an existing enum value
- **THEN** OpenAPI and API response documentation MUST describe how queue/cancel detail is exposed without adding a new domain status

### Requirement: PostgreSQL-valid automatic retry status transitions
Automatic retry operations SHALL write only statuses accepted by production PostgreSQL enum-backed tables.

#### Scenario: Retry status contract is tested against migrations
- **WHEN** tests validate retry status transitions
- **THEN** they MUST inspect or exercise the production migration enum values instead of relying only on SQLite `TEXT` columns
