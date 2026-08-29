## ADDED Requirements

### Requirement: Slurm scheduler service auth context
Slurm mutation enforcement SHALL derive a route-scoped backend auth context from a dedicated scheduler bearer credential without making `services` depend on `apps.api`.

#### Scenario: Valid scheduler service credential
- **WHEN** a Slurm submit or cancel request carries an `Authorization: Bearer` value that constant-time matches configured `SLURM_GATEWAY_SERVICE_TOKEN`
- **THEN** the route derives the fixed scheduler actor with `operator` role and evaluates the canonical Slurm action
- **AND** the credential is not accepted by unrelated business mutation routes.

#### Scenario: Missing, malformed, or mismatched service credential
- **WHEN** a Slurm mutation has no otherwise-valid dev/live identity and the service bearer is missing, mismatched, contains whitespace, or contains non-ASCII input
- **THEN** the route returns `401 AUTH_REQUIRED` and records a no-mutation decision without escaping as an unhandled exception
- **AND** a non-ASCII configured token is treated as unusable configuration while usable ASCII credentials are compared in constant time without trimming
- **AND** the route does not parse an invalid mutation body into a gateway call or invoke a gateway method.

#### Scenario: Scheduler token cannot reset registry
- **WHEN** the scheduler service credential calls an enabled internal reset route
- **THEN** policy returns `403 RBAC_FORBIDDEN` because reset requires `sys_admin`
- **AND** the registry remains unchanged.

#### Scenario: Credential material is secret-safe
- **WHEN** configuration, request, client, policy, exception, OpenAPI, log, or receipt data is serialized
- **THEN** the service token value is omitted or redacted
- **AND** only the environment variable and security-scheme names may appear.
