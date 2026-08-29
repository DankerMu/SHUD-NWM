# api-contract-alignment Specification

## Purpose
TBD - created by archiving change m6-system-hardening-alignment. Update Purpose after archive.
## Requirements
### Requirement: OpenAPI matches backend success shapes
`openapi/nhms.v1.yaml` SHALL describe the actual success response shapes returned by backend routes.

#### Scenario: Forecast response contract is explicit
- **WHEN** forecast-series is requested with or without `include_analysis`
- **THEN** OpenAPI MUST document the supported query parameters and response shape, including any spliced analysis/forecast response variant

#### Scenario: Runs response contract matches pagination
- **WHEN** `/api/v1/runs` returns a paginated object
- **THEN** OpenAPI MUST define the page object fields `items`, `total_count` or `total`, `limit`, and `offset`

### Requirement: Frontend uses generated contracts for API calls
Frontend stores SHALL use generated OpenAPI types for stable endpoint payloads and SHALL avoid `unknown` normalization except at intentional compatibility boundaries.

#### Scenario: Monitoring job metadata is typed
- **WHEN** `/api/v1/jobs` includes `run_type` and `scenario`
- **THEN** `PipelineJob` generated types MUST expose those fields without local store type patching

### Requirement: Success envelope policy is uniform or explicitly documented
The project SHALL define whether successful API responses use `{request_id, status, data}` envelopes or raw payloads, and backend, OpenAPI, frontend, and docs SHALL follow that policy.

#### Scenario: Envelope endpoint returns documented envelope
- **WHEN** an endpoint is documented as enveloped
- **THEN** its successful response body MUST include `request_id`, `status`, and `data`

#### Scenario: Raw endpoint is documented as raw
- **WHEN** an endpoint intentionally returns a raw payload
- **THEN** OpenAPI and docs MUST document the raw payload and MUST NOT claim a success envelope for that endpoint

### Requirement: API contract tests protect representative endpoints
The automated test suite SHALL validate representative backend responses against OpenAPI or an equivalent contract fixture.

#### Scenario: Endpoint matrix is covered
- **WHEN** the contract suite runs
- **THEN** it MUST cover forecast-series, runs, jobs, monitoring metrics, and queue depth endpoint categories with OpenAPI schemas, generated frontend types, backend route tests, and frontend store tests where applicable

#### Scenario: Backend response drift fails CI
- **WHEN** a backend route changes response field names without updating OpenAPI
- **THEN** a contract test MUST fail

#### Scenario: Frontend generated types are current
- **WHEN** OpenAPI changes
- **THEN** frontend type generation MUST be run and CI MUST fail if generated types are stale

### Requirement: Published OpenAPI MUST be valid for its declared dialect

The runtime `/openapi.json` document and `openapi/nhms.v1.yaml` MUST declare the same OpenAPI 3.1 version, MUST be semantically equal, and MUST contain no OpenAPI-3.0-only `nullable` keyword. Every value accepted as null before this repair MUST remain nullable through JSON Schema 3.1 union semantics, and unchanged business schemas MUST generate byte-identical frontend TypeScript.

#### Scenario: Ordinary nullable schema preserves its contract

- **WHEN** a hand-patched schema with an ordinary scalar, array, or object `type` is nullable
- **THEN** the published schema contains a 3.1 type union with `null`, preserves format/description/items/bounds/additional-properties siblings, and generates the same TypeScript value-or-null type

#### Scenario: Composed nullable schema preserves its contract

- **WHEN** the nullable `Layer.metadata` schema combines a type with `allOf`
- **THEN** the published schema expresses the complete composition or null without producing an extra intersection, and generated TypeScript remains `LayerMetadata | null`

#### Scenario: Runtime and static contract cannot diverge

- **WHEN** either the patch owner or static YAML changes without the other
- **THEN** the exact runtime/static drift test fails and the artifact cannot be accepted

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
