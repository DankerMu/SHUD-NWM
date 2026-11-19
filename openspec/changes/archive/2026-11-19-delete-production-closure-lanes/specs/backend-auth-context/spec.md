## REMOVED Requirements

### Requirement: Backend Auth Context

**Reason**: Replaced by "Backend auth context for protected requests": the readiness validation (`validate-readiness`) scenario of this requirement are cut (#3221), and OpenSpec refuses a MODIFIED block that drops a scenario, so the surviving text moves to a new header.
**Migration**: See the ADDED requirement "Backend auth context for protected requests"; apart from those cuts its body and scenarios are unchanged.

## ADDED Requirements

### Requirement: Backend auth context for protected requests
The API SHALL derive a backend auth context for every protected request, including actor id, role set, auth mode, and whether live backend auth was executed.

#### Scenario: Dev/test token accepted
WHEN a request supplies a configured dev/test auth token or role header in non-production mode
THEN protected endpoints receive actor, roles, `auth_mode=dev_test`, and `live_backend_auth_executed=false`.

#### Scenario: Missing credentials
WHEN a protected endpoint receives no valid auth context
THEN the API returns a stable unauthorized error and does not call the underlying service action.

#### Scenario: Dev override rejected in production mode
WHEN a production-mode request supplies only a dev/test role override header
THEN the API rejects it with `401 AUTH_REQUIRED` and `live_backend_auth_executed=false`.

#### Scenario: Live credential role mapping fails
WHEN a live IdP credential is present but maps to no accepted role
THEN the API returns `403 RBAC_FORBIDDEN`, records the failed role mapping, and does not call the protected service action.
