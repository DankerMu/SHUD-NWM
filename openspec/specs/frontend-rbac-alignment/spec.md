# frontend-rbac-alignment Specification

## Purpose
TBD - created by archiving change m17-production-auth-rbac. Update Purpose after archive.
## Requirements
### Requirement: Frontend RBAC Alignment
Frontend gates SHALL match backend policy decisions and clearly distinguish local dev/test role override from production auth.

#### Scenario: Role-gated actions
WHEN a user has viewer/analyst/operator/model_admin/sys_admin roles
THEN visible actions match backend-authorized capabilities for monitoring, model assets, and production operations.

#### Scenario: Backend denies visible action
WHEN the frontend shows an action but backend policy denies it
THEN the UI displays the stable forbidden error and refreshes state without assuming success.

#### Scenario: Dev override enabled
WHEN local role override is enabled in development/test mode
THEN the UI displays test-only role control behavior and does not label it as production authentication.

