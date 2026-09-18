## ADDED Requirements

### Requirement: The guard's shell test harness SHALL serialize its tool-call document as JSON

Every tool-call document the large-file-guard shell test harness feeds to the hook SHALL be produced by a single
JSON serializer over the whole document, never by interpolating a field's raw bytes into a JSON string literal.
A harness that interpolates `command` raw cannot express a command containing a double quote, backslash, newline,
carriage return, or tab: the resulting bytes are not valid JSON, the hook dies while parsing, and the guard logic
under test never executes — so the harness silently loses coverage of exactly the inputs most likely to break
quoting. The hook itself is not in scope; real tool-call payloads already encode every field.

#### Scenario: A command bearing quotes and control characters reaches the guard logic

- **GIVEN** a test case whose command contains a double quote, a backslash, a newline, a carriage return, and a tab
- **WHEN** the harness feeds the resulting document to the hook
- **THEN** the hook parses the document and returns a guard verdict, rather than failing during JSON parsing

#### Scenario: No call site builds the document by raw interpolation

- **GIVEN** the harness's helpers and its directly hand-written cases
- **WHEN** their document construction is inspected
- **THEN** every one of them routes through the single serializer, so no raw interpolation slot remains
