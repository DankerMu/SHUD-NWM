# Spec Delta: hypertable-compression

## ADDED Requirements

### Requirement: The recovery-target six-field contract MUST have a single Python source bound to the schema consts by an automated guard

The recovery target SHALL be defined as one six-field contract
(hypertable schema and name, chunk schema and name, range start and
end) with a single Python source of truth; the supervisor expected
decompress argv and the capture evidence target SHALL derive from that
source, and an automated guard SHALL assert field-by-field equality
between the source and every remaining production copy — the schema
consts that pin the same values, the synthetic
`decompress_return_relation` const, and the verifier's own
recovery-target oracle — so that mutating any single copy fails a
test instead of shipping a half-migrated target. Copies that cannot
derive from the source directly MAY instead be covered by a test whose
failure is triggered by a one-sided change (direct equality assertion
or a gate test the copy flows through).

#### Scenario: All six schema consts and the verifier oracle are bound to the shared source

- **WHEN** the drift-guard test compares the schema
  `recovery_target` consts, `decompress_return_relation`, and the
  verifier's recovery-target oracle against the shared contract
  constants
- **THEN** all six fields and the synthetic `chunk_schema.chunk_name`
  relation match field-by-field, and changing any one copy alone makes
  a test fail

#### Scenario: Supervisor argv and capture evidence derive from the source

- **WHEN** the supervisor builds its expected decompress argv and the
  capture producer emits its recovery-target evidence and preflight SQL
- **THEN** the chunk schema, chunk name, range start, and range end come
  from the shared constants and their rendered content is unchanged for
  the pinned values
