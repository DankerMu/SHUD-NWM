## ADDED Requirements

### Requirement: Error responses SHALL leave a redacted server-side log line

`error_response()` SHALL emit one log line per error response containing the `request_id`, `code`, `status_code`, request path and the `details` payload after `redact_audit_payload` plus key-level redaction of `rejected_value`, rendered in the message text; the API process SHALL install a stderr handler so the line reaches the unit's log under the production uvicorn configuration. Logging SHALL never change the response.

#### Scenario: Concurrent-replace 500 is findable afterwards

- **WHEN** a `STATION_FORCING_FILE_MALFORMED` error with `parse_reason: concurrent-replace: …` and an absolute `expected_path` is returned
- **THEN** the log text contains the same `request_id` as the `X-Request-ID` header, the code, and `concurrent-replace:` verbatim
- **AND** `expected_path` is rendered as `[redacted]`

#### Scenario: Validation errors are covered

- **WHEN** a request fails `RequestValidationError` outside the `/api/v1/slurm` prefix
- **THEN** a line with the validation code is logged and every `rejected_value` renders as `[redacted]` regardless of its shape

#### Scenario: Logging cannot break the response

- **WHEN** `details` carries a value the redactor cannot process
- **THEN** the response is returned unchanged and the line falls back to a safe representation
