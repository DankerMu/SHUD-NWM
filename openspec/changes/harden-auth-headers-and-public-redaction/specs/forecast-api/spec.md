## ADDED Requirements

### Requirement: Hindcast series SHALL NOT be gated by a client-asserted role header

`GET /api/v1/basin-versions/{basin_version_id}/river-segments/{segment_id}/forecast-series` SHALL serve
`run_types` containing `hindcast` under the same anonymous access as every other run type, matching its
OpenAPI root `security: []` declaration. The route SHALL NOT read `X-User-Role` or any other
client-asserted identity header; identity headers are honoured only through the shared request auth context.

#### Scenario: Hindcast request without identity headers succeeds

- **WHEN** a client requests forecast-series with `run_types=hindcast` and no identity headers
- **THEN** the response is 200 and the store query receives `hindcast` in `run_types`

#### Scenario: Asserted role header has no effect

- **WHEN** a client sends `X-User-Role: viewer` or an arbitrary value with `run_types=hindcast`
- **THEN** the response is the same 200 body as without the header
