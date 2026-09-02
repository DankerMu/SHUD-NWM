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

### Requirement: Declared error responses cite reachable raise sites

Every error response declared on an operation in `openapi/nhms.v1.yaml` SHALL correspond to a status code the runtime can actually return from that operation, and the runtime schema patch in `apps/api/openapi_patching.py` SHALL declare the same response so `tests/test_openapi_drift.py` keeps static and runtime equal. An operation with no reachable 4XX SHALL NOT declare one to satisfy a lint rule; the retained `operation-4xx-response` warning is recorded in the change's `tasks.md` with the raise-site audit that justifies it. No lint rule is skipped or ignored to reduce the warning count.

#### Scenario: Queue depth declares its reachable gateway failures

- **WHEN** the static contract and `app.openapi()` are compared for `GET /api/v1/queue/depth`
- **THEN** both declare `503` with error code `CONTROL_PLANE_QUEUE_UNAVAILABLE`, `502` with error codes `SLURM_COMMAND_ERROR` and `SLURM_PARSE_ERROR`, and `504` with error code `SLURM_TIMEOUT`
- **AND** `tests/test_api_contract.py` asserts the three code sets are equal between static and runtime

#### Scenario: Operations without a reachable 4XX declare none

- **WHEN** `redocly lint openapi/nhms.v1.yaml --skip-rule no-unused-components` is run at the pinned CLI version
- **THEN** it reports 0 errors
- **AND** the only warnings are `operation-4xx-response` on `GET /api/v1/queue/depth`, `GET /api/v1/slurm/health`, `GET /health`, and `info-license` on `#/info`
- **AND** `tasks.md` records the per-operation reason no 4XX is reachable and that `info.license` awaits an owner decision

