## MODIFIED Requirements

### Requirement: Retry and cancel actions
The system SHALL render cancel buttons for active jobs, using the actual user role in the X-User-Role header. It SHALL NOT render a retry button for any job (#3179); failed-run recovery is the node-22 manual-repair marker lane.

#### Scenario: Retry with actual role
- **WHEN** the user with role "operator" views a failed job, or clicks cancel on an active job
- **THEN** no retry button is rendered for the failed job
- **AND** the cancel request MUST include header `X-User-Role: operator` (not a hard-coded value)

#### Scenario: Viewer cannot retry
- **WHEN** the user role is "viewer"
- **THEN** cancel buttons MUST NOT be rendered, and no retry button exists for any role
