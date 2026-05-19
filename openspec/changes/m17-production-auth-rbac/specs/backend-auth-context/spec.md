## ADDED Requirements

### Requirement: Backend Auth Context
The API SHALL derive a backend auth context for every protected request, including actor id, role set, auth mode, and whether live backend auth was executed.

#### Scenario: Dev/test token accepted
WHEN a request supplies a configured dev/test auth token or role header in non-production mode
THEN protected endpoints receive actor, roles, `auth_mode=dev_test`, and `live_backend_auth_executed=false`.

#### Scenario: Missing credentials
WHEN a protected endpoint receives no valid auth context
THEN the API returns a stable unauthorized error and does not call the underlying service action.

#### Scenario: Live IdP not configured
WHEN production readiness validation runs without live IdP configuration
THEN evidence records a release blocker instead of claiming production auth passed.

#### Scenario: Dev override rejected in production mode
WHEN a production-mode request supplies only a dev/test role override header
THEN the API rejects it with `401 AUTH_REQUIRED` and `live_backend_auth_executed=false`.

#### Scenario: Live credential role mapping fails
WHEN a live IdP credential is present but maps to no accepted role
THEN the API returns `403 RBAC_FORBIDDEN`, records the failed role mapping, and does not call the protected service action.
