## ADDED Requirements

### Requirement: The precipitation index error code SHALL come only from the API error envelope
When the precipitation index request fails, the overview store SHALL read the error code only from the envelope's `error.code` string. A top-level `code`, a non-object `error`, or a non-string `error.code` SHALL be treated as no code, so the cycle's index state becomes `error`. Store-level tests SHALL pin each of these shapes.

#### Scenario: Top-level code is ignored
- **WHEN** the precipitation index response body is `{ "code": "PRECIP_CYCLE_NOT_MIRRORED", "message": "..." }`
- **THEN** the cycle's index state is `error`, not `not_mirrored`

#### Scenario: Envelope code is honoured
- **WHEN** the response body is `{ "error": { "code": "PRECIP_CYCLE_NOT_MIRRORED" } }`
- **THEN** the cycle's index state is `not_mirrored`
