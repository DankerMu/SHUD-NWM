# api-contract-convergence Specification

## Purpose
TBD - created by archiving change m7-second-review-remediation. Update Purpose after archive.
## Requirements
### Requirement: OpenAPI path and server prefix convergence
The OpenAPI document SHALL use exactly one API prefix strategy.

#### Scenario: Full paths are used
- **WHEN** OpenAPI paths include `/api/v1`
- **THEN** the `servers` URL MUST NOT also add `/api/v1`
- **AND** generated clients MUST call implemented backend routes without double-prefixing

#### Scenario: Server prefix is used
- **WHEN** OpenAPI `servers` contains `/api/v1`
- **THEN** paths MUST be relative to that server prefix
- **AND** generated frontend route types MUST still match backend routes

### Requirement: Backend route behavior matches OpenAPI
Every implemented public API endpoint used by the frontend or documented as supported SHALL match OpenAPI request and response shapes.

#### Scenario: Data source endpoint returns documented envelope
- **WHEN** `GET /api/v1/data-sources` is called
- **THEN** its response shape MUST match the OpenAPI schema, including whether a success envelope is used

#### Scenario: Model active request body matches schema
- **WHEN** `PUT /api/v1/models/{model_id}/active` is called
- **THEN** the accepted request body field name MUST match OpenAPI and generated frontend types
- **AND** compatibility for any renamed field MUST be documented and tested

#### Scenario: Forecast series include analysis parameters are documented
- **WHEN** `forecast-series` supports `include_analysis` or `run_types`
- **THEN** OpenAPI MUST include those query parameters
- **AND** the response schema MUST cover the raw or enveloped shape returned by both forecast-only and spliced analysis+forecast cases

### Requirement: Implemented and documented route sets are reconciled
The repository SHALL detect public route drift between FastAPI and `openapi/nhms.v1.yaml`.

#### Scenario: Documented route is missing
- **WHEN** OpenAPI lists a public route that FastAPI does not implement
- **THEN** a contract test MUST fail unless the route is explicitly marked deferred or non-generated

#### Scenario: Implemented route is undocumented
- **WHEN** FastAPI exposes a public `/api/v1` route
- **THEN** a contract test MUST fail unless the route is explicitly internal or excluded from the public contract

#### Scenario: Known second-review drift endpoints are not hidden by allowlists
- **WHEN** route drift tests use an allowlist for deferred or internal endpoints
- **THEN** the test MUST explicitly account for lineage, layers, model detail, station series, river-network tiles, hydro tiles, met tiles, state snapshots, and Slurm endpoints
- **AND** the allowlist MUST distinguish implemented-internal routes from documented-but-deferred routes

### Requirement: Frontend API base configuration is executable
Frontend API base URL documentation SHALL match runtime client behavior.

#### Scenario: Environment API base is documented
- **WHEN** `.env.example` or README documents a frontend API base variable
- **THEN** `apps/frontend/src/api/client.ts` MUST read and apply that variable without double-prefixing paths

#### Scenario: Frontend types are regenerated
- **WHEN** OpenAPI changes
- **THEN** `apps/frontend/src/api/types.ts` MUST be regenerated
- **AND** CI MUST fail if committed generated types differ from the current OpenAPI output

