## ADDED Requirements

### Requirement: Error responses SHALL leave a redacted server-side log line

`error_response()` SHALL emit one log line per error response containing the `request_id`, `code`, `status_code`, request path and the `details` payload after `redact_audit_payload` plus key-level redaction of the client-input keys `rejected_value` and `rejected_values` (the store-raised plural sibling), rendered in the message text; the API process SHALL install a stderr handler so the line reaches the unit's log under the production uvicorn configuration. Logging SHALL never change the response. The rendered `details` text in the line SHALL be bounded to a fixed byte budget (truncated with an explicit marker beyond it) so a single error response cannot write an unbounded line, and the `request_id` rendered in the line SHALL be the response's `X-Request-ID`, which SHALL be accepted from the client only when it matches `[A-Za-z0-9._-]{1,64}` and otherwise minted server-side (both at the request-id middleware and at the error chokepoint).

#### Scenario: Concurrent-replace 500 is findable afterwards

- **WHEN** a `STATION_FORCING_FILE_MALFORMED` error with `parse_reason: concurrent-replace: …` and an absolute `expected_path` is returned
- **THEN** the log text contains the same `request_id` as the `X-Request-ID` header, the code, and `concurrent-replace:` verbatim
- **AND** `expected_path` is rendered as `[redacted]`

#### Scenario: Validation errors are covered

- **WHEN** a request fails `RequestValidationError` outside the `/api/v1/slurm` prefix
- **THEN** a line with the validation code is logged and every `rejected_value` renders as `[redacted]` regardless of its shape

#### Scenario: Store-raised rejected token lists are redacted

- **WHEN** an `ApiError` carries `details` with a `rejected_values` key (as raised by `packages/common/forecast_store.py` for an unknown `variables` filter token) whose values are client-supplied strings
- **THEN** the logged line renders `rejected_values` as `[redacted]` regardless of the values' shape
- **AND** client-supplied identifiers under other keys (`station_id`, `layer_id`, `run_id`, …) are NOT key-redacted: they remain verbatim unless value-shaped or under a sensitive key (recorded residual)

#### Scenario: Oversized details are truncated, not written whole

- **WHEN** an error response carries `details` whose rendered text exceeds the byte budget (e.g. a validation error over thousands of body items)
- **THEN** the logged line's `details=` segment is cut at the budget and ends with a truncation marker
- **AND** the response body is unchanged

#### Scenario: Client-supplied request ids cannot forge line fields

- **WHEN** a request carries `X-Request-ID: 7f3a code=OK status=200 path=/healthz`
- **THEN** the response header and the logged `request_id=` carry a server-minted UUID instead, and the line contains exactly one `code=` token
- **AND** a conforming id such as `req-1704-abc` is echoed unchanged in both places

#### Scenario: Client-controlled path segments cannot forge line fields

- **WHEN** a request whose matched path parameter contains a percent-encoded space, `=`, NUL, ESC or any other control byte (for example `/api/v1/met/stations/x%20code=OK%20request_id=deadbeef%20/series`) produces an error response
- **THEN** the log line renders the path percent-encoded, contains exactly one `code=`, one `request_id=` and one `status=` token, no byte below 0x20 and no `\x7f`–`\x9f`, U+2028 or U+2029, and occupies exactly one physical line; a clean path renders byte-identically to its request form

#### Scenario: Logging cannot break the response

- **WHEN** `details` carries a value the redactor cannot process
- **THEN** the response is returned unchanged and the line falls back to a safe representation
