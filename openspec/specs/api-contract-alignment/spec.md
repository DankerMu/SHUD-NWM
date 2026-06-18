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

#### Scenario: Flood alert response contract matches implementation
- **WHEN** flood alert summary, ranking, segments, or timeline endpoints return data
- **THEN** OpenAPI schemas MUST include the implemented fields such as `levels`, `usable_curves`, `unavailable_count`, `quality_note`, `items`, `total`, `limit`, and `offset`

### Requirement: Frontend uses generated contracts for API calls
Frontend stores SHALL use generated OpenAPI types for stable endpoint payloads and SHALL avoid `unknown` normalization except at intentional compatibility boundaries.

#### Scenario: Generated flood alert types compile
- **WHEN** OpenAPI is regenerated
- **THEN** flood alert frontend stores and tests MUST compile against generated summary, ranking, segments, and timeline schemas

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
- **THEN** it MUST cover forecast-series, runs, jobs, monitoring metrics, queue depth, and flood-alert summary/ranking/segments/timeline endpoint categories with OpenAPI schemas, generated frontend types, backend route tests, and frontend store tests where applicable

#### Scenario: Backend response drift fails CI
- **WHEN** a backend route changes response field names without updating OpenAPI
- **THEN** a contract test MUST fail

#### Scenario: Frontend generated types are current
- **WHEN** OpenAPI changes
- **THEN** frontend type generation MUST be run and CI MUST fail if generated types are stale

