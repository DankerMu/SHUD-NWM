## REMOVED Requirements

### Requirement: PostgreSQL-valid retry status transitions

**Reason**: Scenario "Manual retry updates hydro run with valid enum value" is driven through `POST /api/v1/runs/{run_id}/retry`; the only writer of the retry `hydro.hydro_run.status = 'pending'` reset was the retired `RetryService.attempt_manual_retry` (retry.py post-submit UPDATE). The route now writes nothing. The migration-contract scenario is carried over under a new name.

**Migration**: See ADDED "PostgreSQL-valid automatic retry status transitions".

## ADDED Requirements

### Requirement: PostgreSQL-valid automatic retry status transitions
Automatic retry operations SHALL write only statuses accepted by production PostgreSQL enum-backed tables.

#### Scenario: Retry status contract is tested against migrations
- **WHEN** tests validate retry status transitions
- **THEN** they MUST inspect or exercise the production migration enum values instead of relying only on SQLite `TEXT` columns
