## ADDED Requirements

### Requirement: Public route drift is explicitly resolved

OpenAPI and FastAPI public routes SHALL match except for narrowly documented future or internal routes.

#### Scenario: Drift allowlists are minimal and justified

- **WHEN** the OpenAPI drift test runs
- **THEN** every documented-but-missing route is either implemented or listed with an issue-scoped deferral comment
- **AND** every implemented-but-undocumented route is either documented in OpenAPI or listed as internal with a reason

### Requirement: Model list contract is aligned

`GET /api/v1/models` SHALL have the same runtime response shape, OpenAPI schema, and generated frontend type.

#### Scenario: Model list response matches schema

- **WHEN** a client calls `GET /api/v1/models?active=all&limit=10&offset=0`
- **THEN** the response body MUST match the OpenAPI schema
- **AND** the model items and pagination fields MUST be represented consistently in generated frontend types

#### Scenario: Active query accepts documented values

- **WHEN** a client calls `GET /api/v1/models?active=true`, `active=false`, or `active=all`
- **THEN** the backend MUST apply the documented filter semantics
- **AND** OpenAPI MUST document exactly those accepted values or an intentionally narrower matching contract

#### Scenario: Omitted active query preserves default

- **WHEN** a client calls `GET /api/v1/models` without an `active` query parameter
- **THEN** the backend MUST preserve the documented default filter behavior
- **AND** tests MUST state whether active-only or all models are returned by default

### Requirement: Model registry errors use the API error envelope

Model registry routes SHALL return the project error envelope for known registry failures.

#### Scenario: Model route returns structured error

- **WHEN** a model registry route rejects a duplicate, missing resource, invalid reference, invalid payload, or package validation error
- **THEN** the response MUST include `request_id`, `status="error"`, and `error.code`
- **AND** the HTTP status MUST preserve the intended duplicate/not-found/validation/server-error category

#### Scenario: Model error codes are stable

- **WHEN** duplicate, missing resource, invalid reference, invalid payload, package validation, or generic registry errors are raised
- **THEN** tests MUST assert the HTTP status, `error.code`, message/details shape, and request ID presence for each class

### Requirement: Flood threshold schemas generate useful types

Flood timeline and forecast threshold schemas SHALL describe concrete threshold maps or objects rather than empty object schemas.

#### Scenario: Generated flood threshold type is not empty

- **WHEN** frontend API types are generated from OpenAPI
- **THEN** flood timeline `frequency_thresholds` and forecast `frequency_thresholds` MUST not become `Record<string, never>`
- **AND** the generated type MUST allow the threshold keys or numeric additional properties returned by the backend

#### Scenario: Generated flood threshold type names usable values

- **WHEN** generated frontend types are checked
- **THEN** the flood threshold type MUST include concrete threshold properties such as `Q2`, `Q5`, `Q10`, `Q20`, `Q50`, and `Q100`, or an explicit numeric additional-property map
- **AND** no key flood threshold field may be typed only as an empty object
