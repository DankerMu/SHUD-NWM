## MODIFIED Requirements

### Requirement: OpenAPI server and security metadata MUST describe the enforced boundary

The published OpenAPI contract MUST declare the same-origin server and explicit anonymous-by-default root security. Every operation protected by the current auth/RBAC middleware or dependency, across every supported HTTP method including DELETE, MUST override that root with the conditional credential alternatives actually accepted by the server; public operations MUST NOT inherit a false global authentication requirement. Security descriptions MUST state their environment conditions and MUST NOT contain credential values.

#### Scenario: Public operation remains public by default

- **WHEN** a caller or generator inspects one of the 39 operations that remain unprotected after the four Slurm mutations become protected
- **THEN** it inherits root `security: []` and no global bearer requirement is asserted
- **AND** Slurm health, list, status, array-result, and log reads remain in this anonymous set.

#### Scenario: Existing business mutation keeps its exact identity alternatives

- **WHEN** a caller or generator inspects any of the original 11 protected mutation/retry/cancel operations
- **THEN** that operation overrides root security with only the enabled non-production role header, configured non-production bearer token, or complete internal live-proof header set
- **AND** `SlurmServiceBearer` is not an accepted alternative for those operations.

#### Scenario: Slurm mutation publishes its exact identity alternatives

- **WHEN** a caller or generator inspects `POST /api/v1/slurm/jobs`, `POST /api/v1/slurm/job-arrays`, `DELETE /api/v1/slurm/jobs/{job_id}`, or `POST /api/v1/slurm/internal/reset`
- **THEN** that operation overrides root security with the original three identity alternatives plus `SlurmServiceBearer`
- **AND** the service bearer description names its route scope and configuration condition without embedding a token.

#### Scenario: Runtime authorization behavior matches the two documented sets

- **WHEN** anonymous, unauthorized, release-blocked, service-bearer, or authorized requests reach protected operations
- **THEN** the runtime 401/403/503/allow and no-mutation decisions match the applicable documented security set
- **AND** a service bearer can authorize only Slurm submit/cancel actions, never an original business mutation or Slurm reset.

#### Scenario: Runtime, static, and generated contracts cannot diverge

- **WHEN** enforcement, runtime OpenAPI, static YAML, or generated frontend types change independently
- **THEN** contract tests compare every supported HTTP method, including DELETE, and fail unless the documented protected-operation set equals the enforced set
- **AND** static YAML remains exactly equal to runtime OpenAPI and generated types remain current.

#### Scenario: Contract contains no secret

- **WHEN** security schemes and operation requirements are serialized
- **THEN** only scheme/header names and conditions appear, and no configured token, actor, credential, or signed value is embedded.
