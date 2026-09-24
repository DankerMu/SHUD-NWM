## ADDED Requirements

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
