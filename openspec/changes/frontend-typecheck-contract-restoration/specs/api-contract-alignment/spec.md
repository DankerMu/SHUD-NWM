## MODIFIED Requirements

### Requirement: Frontend uses generated contracts for API calls
Frontend stores SHALL use generated OpenAPI types for stable endpoint payloads and SHALL avoid `unknown` normalization except at intentional compatibility boundaries. Every response body a frontend surface consumes through a `components['schemas'][...]` reference SHALL be described by a **named** component schema in `openapi/nhms.v1.yaml`, not by an anonymous `type: object` with `additionalProperties: true`, and every such reference SHALL resolve in the generated `apps/frontend/src/api/types.ts`.

#### Scenario: Monitoring job metadata is typed
- **WHEN** `/api/v1/jobs` includes `run_type` and `scenario`
- **THEN** `PipelineJob` generated types MUST expose those fields without local store type patching

#### Scenario: Consumed response bodies resolve to named schemas
- **WHEN** a frontend module references `components['schemas'][X]` for a response body
- **THEN** `X` MUST exist as a named schema in `openapi/nhms.v1.yaml`, MUST be reachable from that route's declared `200` response, and MUST resolve in the generated `types.ts` so a full-program type check compiles the reference

#### Scenario: A frontend call with no backend route is not documented as one
- **WHEN** a frontend module calls an endpoint that no backend route implements
- **THEN** the project MUST NOT invent an OpenAPI schema for it; the consumer MUST declare the shape locally and the missing route MUST be tracked as a separate defect
