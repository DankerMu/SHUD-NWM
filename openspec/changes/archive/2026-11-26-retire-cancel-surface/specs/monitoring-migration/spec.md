## REMOVED Requirements

### Requirement: Retry and cancel actions

**Reason**: The control-plane cancel route is retired (#3394) with its button; the scenario about the cancel request's role header goes. OpenSpec refuses a MODIFIED block that drops a scenario, so the surviving text moves to a new header.
**Migration**: See the ADDED requirement "The jobs table SHALL render no run-action control".

## ADDED Requirements

### Requirement: The jobs table SHALL render no run-action control
The system SHALL NOT render a retry button (#3179) or a cancel button (#3394) for any job, in any role and any service role, and SHALL NOT send a retry or cancel request. The action column keeps the log modal and the diagnostics actions. Failed-run recovery is the node-22 manual-repair marker lane.

#### Scenario: Operator sees no run action
- **WHEN** a user with role "operator" views a failed job and an active job on a `compute_control` or `dev_monolith` API
- **THEN** no retry button and no cancel button is rendered for either
- **AND** the log action is rendered for both

#### Scenario: Viewer sees no run action
- **WHEN** the user role is "viewer"
- **THEN** no retry button and no cancel button is rendered
