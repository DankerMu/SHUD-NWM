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

### Requirement: Display lineage proposals are not delivered API promises
Current API documentation SHALL distinguish unimplemented display lineage proposals from registered, supported API endpoints. It SHALL NOT claim a delivered river-point/forcing-point/product lineage API or a display lineage UI after retirement of the basin detail lane. Existing model-asset provenance SHALL remain unaffected.

#### Scenario: Reader checks display lineage support
- **WHEN** a reader consults docs/spec/04_api_design.md section 9
- **THEN** the document explicitly states that all three proposed display lineage routes are unimplemented and the prior frontend call was removed
- **AND** no conflicting flat-object or nodes/edges response is presented as a current supported contract

### Requirement: JSON routes declare a runtime response model

Every JSON route in `apps/api/routes/` SHALL declare an explicit Pydantic
`response_model`. Routes that return `Response` (tiles, PNG) are exempt, and
routes outside `apps/api/routes/` are out of scope. Adding a model SHALL NOT
change a route's parsed JSON response relative to the implicit
`dict[str, Any]` response field. The comparison is type-strict: the same keys,
the same distinction between an absent key and `null`, and the same values with
the same JSON types. The only exception is a filtering model that its own
requirement names, such as `HydroRun`. Where a route's published OpenAPI data
schema is a hand-written schema, a test SHALL bind it to the model: the same
property-name set and the same `required` set at every object level. Where no
hand-written schema exists, the model's generated schema SHALL be published
in the layout the handler actually returns: inside the `SuccessEnvelope` layout
for enveloped routes, and at the response root for routes that return no
envelope. When a response fails model validation, the
API SHALL return the standard error envelope with a `request_id` and the code
`RESPONSE_VALIDATION_ERROR`, and log one redacted server-side line that
excludes the response payload.

#### Scenario: a route payload passes through its response model

- **WHEN** a representative handler payload (the Python objects the handler
  returns) for any modelled route is serialized through the new model field and
  through the implicit `dict[str, Any]` field
- **THEN** both parse to the same JSON value with the same type at every path,
  except for keys that a named filtering model removes.

#### Scenario: a new JSON route is added without a model

- **WHEN** a JSON route in `apps/api/routes/` is registered without an explicit
  `response_model`
- **THEN** the route-table completeness test fails.

#### Scenario: a published hand schema drifts from its model

- **WHEN** a model field is added, removed or made optional without the same
  change to the hand-written published schema
- **THEN** the schema parity test fails.
