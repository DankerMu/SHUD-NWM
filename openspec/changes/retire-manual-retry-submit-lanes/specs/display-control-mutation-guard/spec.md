## REMOVED Requirements

### Requirement: Compute control preserves existing behavior

**Reason**: Scenario "Compute retry still submits" asserts that the existing retry service submits through the configured gateway on a compute-control API; the route no longer submits (issue #3157). A scenario cannot be dropped in place, so the requirement is re-added under a new name. The cancel scenario is carried over verbatim.

**Migration**: See ADDED "Compute control preserves cancel behavior and fails retry closed".

## ADDED Requirements

### Requirement: Compute control preserves cancel behavior and fails retry closed

The compute control and dev monolith roles SHALL preserve the existing cancel behavior and authorization checks, and SHALL answer retry with the same stable manual-action error as display mode instead of submitting work.

#### Scenario: Compute retry fails closed
- **WHEN** an authorized caller posts retry on a compute-control or dev-monolith API
- **THEN** the API returns HTTP `409` with code `CONTROL_PLANE_MANUAL_ACTION_REQUIRED` in the standard API error envelope, including when no `DATABASE_URL` is configured
- **AND** no retry service, database session, or Slurm gateway is constructed and no `submit_job` call is made
- **AND** the response does not claim a new job was submitted.

#### Scenario: Compute cancel still records proven cancellation
- **WHEN** an authorized caller posts cancel on a compute-control API and the gateway returns proven cancellation
- **THEN** active jobs can transition to cancelled according to the existing cancellation contract
- **AND** unproven cancellation gaps still preserve local state.
