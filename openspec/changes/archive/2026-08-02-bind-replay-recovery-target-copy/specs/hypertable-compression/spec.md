# Spec Delta: hypertable-compression

## MODIFIED Requirements

### Requirement: The recovery-target six-field contract MUST have a single Python source bound to the schema consts by an automated guard

The recovery target SHALL be defined as one six-field contract
(hypertable schema and name, chunk schema and name, range start and
end) with a single Python source of truth; the supervisor expected
decompress argv and the capture evidence target SHALL derive from that
source, and an automated guard SHALL assert field-by-field equality
between the source and the bound copy set — the schema consts that pin
the same values, the synthetic `decompress_return_relation` const, and
the verifier's recovery-target module constants — so that mutating any
copy in that set alone fails a test instead of shipping a half-migrated
target. Copies that cannot derive from the source directly MAY instead
be covered by a test whose failure is triggered by a one-sided change
(direct equality assertion or a gate test the copy flows through), and
any copy known to remain outside the bound set MUST be named in the
source-of-truth documentation with a tracked follow-up.

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

#### Scenario: The capture catalog_post SQL derives from the shared source

- **WHEN** the capture producer renders its catalog_post SQL for the
  pinned recovery target
- **THEN** the six identity fields are interpolated from the derived
  recovery-target mapping, the rendered string is byte-identical to the
  pre-derivation literal, and the `capture:catalog_post` marker remains
  the first token

#### Scenario: The verifier decompress argv tail derives from its own bound constant

- **WHEN** the verifier validates a decompress invocation argv
- **THEN** the expected tail is built from the verifier's own
  recovery-target constant (itself guard-bound to the shared source),
  an argv derived from that constant is accepted, and an argv whose
  tail deviates in any single recovery-target field is rejected

#### Scenario: The replay producer's target derives from the shared source

- **WHEN** the drift-guard test compares the replay producer's module
  `TARGET` mapping and its synthetic `TARGET_RELATION` string against
  the shared contract constants
- **THEN** the six fields and the `chunk_schema.chunk_name` relation
  match field-by-field, the replay producer's own receipt test asserts
  the published target against the contract-derived expectation rather
  than the producer's own constant, and reverting the producer's
  derivation to an independent drifted literal makes a test fail
