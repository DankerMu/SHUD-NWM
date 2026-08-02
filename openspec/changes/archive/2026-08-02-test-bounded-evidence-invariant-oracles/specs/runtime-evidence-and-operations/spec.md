# Spec Delta: runtime-evidence-and-operations

## ADDED Requirements

### Requirement: Bounded-evidence last-line invariants MUST have regression coverage

Two terminal bounded-evidence semantics SHALL be pinned by unit tests —
the terminal limit compaction retaining `limit.reason`, and the hard
evidence-size bound's exact boundary — exercising the real
production functions: a payload penetrating to the terminal
limit-compaction tier must still carry
`limit.reason == "evidence_size_limit_exceeded"` in the emitted
artifact, and the size-limit serializer must accept a payload of exactly
the configured byte bound while refusing one byte more. Weakening either
construct — dropping `reason` from the terminal keep-set, widening the
bound by one byte, or narrowing the acceptance to strictly below the
bound — SHALL fail the scheduler evidence test suite.

#### Scenario: Terminal compaction keeps the truncation marker

- **WHEN** a payload degrades past every earlier tier and the terminal
  limit-compaction rewrites the `limit` block to its reason-only form
- **THEN** the emitted artifact still carries
  `limit.reason == "evidence_size_limit_exceeded"`, and a keep-nothing
  terminal compaction fails the suite

#### Scenario: Hard bound accepts exactly the bound and refuses one byte more

- **WHEN** a payload serializes to exactly `max_evidence_bytes` bytes
- **THEN** the size-limit serializer accepts it, while a payload
  serializing to exactly one byte more is refused, so both an
  off-by-one widening and a `>=` narrowing of the comparison fail the
  suite
