## ADDED Requirements

### Requirement: Legacy-looking API contracts require consumer inventory

The repository SHALL inventory direct and generated consumers before deprecating or removing any legacy-looking API route.

#### Scenario: endpoint appears obsolete
- **WHEN** an API route looks like an old MVP or compatibility endpoint
- **THEN** maintainers must inventory frontend, backend, OpenAPI, generated type, doc, and test consumers before marking it removal-ready

### Requirement: Replacement endpoint is documented before migration

An API contract retirement SHALL define the replacement route or compatibility policy before current consumers are migrated.

#### Scenario: frontend consumer is moved
- **WHEN** a frontend consumer stops using a compatibility endpoint
- **THEN** the replacement endpoint or compatibility policy is documented in the issue and PR evidence

### Requirement: OpenAPI and generated types stay synchronized

Any API retirement PR SHALL keep OpenAPI and generated frontend types synchronized.

#### Scenario: OpenAPI route changes
- **WHEN** an endpoint is deprecated, replaced, or removed from `openapi/nhms.v1.yaml`
- **THEN** generated frontend types and contract tests are updated in the same implementation slice

### Requirement: Active compatibility endpoints are not deleted as dead code

Compatibility endpoints SHALL NOT be removed while current repository consumers still use them.

#### Scenario: latest-product compatibility endpoint is still consumed
- **WHEN** `/api/v1/mvp/qhh/latest-product` or an equivalent compatibility endpoint is still used by current bootstrap or tests
- **THEN** it remains active contract surface rather than dead code
