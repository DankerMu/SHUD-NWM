## MODIFIED Requirements

### Requirement: OpenAPI success envelope does not conflict with endpoint data schemas

OpenAPI SHALL allow each endpoint to define the actual type of `data` without contradictory `allOf` constraints.

#### Scenario: Array data endpoints validate against OpenAPI

- **WHEN** a success response returns array data
- **THEN** the OpenAPI schema for that response MUST validate the response
- **AND** generated clients MUST see the endpoint-specific array type, not a conflicting envelope object type

#### Scenario: Page data endpoints validate against OpenAPI

- **WHEN** `GET /api/v1/models` returns a page/envelope object
- **THEN** the OpenAPI schema MUST describe that exact page/envelope shape
- **AND** generated frontend types MUST not require a different array-only shape

### Requirement: OpenAPI issue_time documents latest and ISO datetime

OpenAPI SHALL document `issue_time=latest` and ISO datetime values wherever the backend accepts both.

#### Scenario: Issue time generated type includes latest

- **WHEN** generated clients read the OpenAPI parameter for `issue_time`
- **THEN** the generated type MUST allow `latest`
- **AND** it MUST allow ISO datetime strings

### Requirement: API contract changes update generated evidence

OpenAPI contract changes SHALL be accompanied by generated type or contract-test evidence.

#### Scenario: OpenAPI and generated types stay aligned

- **WHEN** OpenAPI changes model list, active query, error response, or flood threshold schemas
- **THEN** checked-in generated frontend API types MUST be regenerated or a test MUST prove no generated change is required
- **AND** CI or tests MUST fail if generated types do not match the checked-in OpenAPI
