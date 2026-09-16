# Spec Delta: node27-pgdata-relocation

## ADDED Requirements

### Requirement: Workload receipts SHALL label their evidence grade by proven admission

The PGDATA workload owner SHALL let an operator request a `live` measurement receipt explicitly, and SHALL label a
receipt `live` only when the same run proved its admission. Without that explicit request the receipt SHALL remain
`isolated`, so an isolated receipt can never be produced while claiming live acceptance.

#### Scenario: A live receipt is requested and admitted

- **WHEN** the operator asks the workload owner for a `live` receipt, the measuring session is proven read-only
  under the display read-only role, and the reviewed SHA equals the HEAD of the checkout that executes the run
- **THEN** the published receipt SHALL record the evidence kind `live` with its `isolated` and `live` markers
  consistent with it
- **AND** every other recorded field except the measurement timestamp SHALL be the same as an isolated run over
  the same frozen inputs and the same reviewed SHA

#### Scenario: A live receipt is requested without proven admission

- **WHEN** a `live` receipt is requested but the session is not read-only, the session role is not the display
  read-only role, or the reviewed SHA is not the executing checkout's HEAD
- **THEN** the run SHALL refuse with a typed code before measuring or publishing
- **AND** no receipt file SHALL exist at the requested output path, not even a partial one

#### Scenario: No evidence grade is requested

- **WHEN** no evidence grade is requested
- **THEN** the receipt SHALL be recorded as `isolated`, exactly as before
