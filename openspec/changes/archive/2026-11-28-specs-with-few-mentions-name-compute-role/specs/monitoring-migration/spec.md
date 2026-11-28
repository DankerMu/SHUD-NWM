## MODIFIED Requirements

### Requirement: The jobs table SHALL render no run-action control
The system SHALL NOT render a retry button (#3179) or a cancel button (#3394) for any job, in any role and any service role, and SHALL NOT send a retry or cancel request. The action column keeps the log modal and the diagnostics actions. Failed-run recovery is the manual-repair marker lane of the compute role.

#### Scenario: Operator sees no run action
- **WHEN** a user with role "operator" views a failed job and an active job on a `compute_control` or `dev_monolith` API
- **THEN** no retry button and no cancel button is rendered for either
- **AND** the log action is rendered for both

#### Scenario: Viewer sees no run action
- **WHEN** the user role is "viewer"
- **THEN** no retry button and no cancel button is rendered
