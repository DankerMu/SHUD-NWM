## ADDED Requirements

### Requirement: Readiness validator entrypoints remain stable
The readiness validation extraction SHALL keep `validate_readiness(config)`,
`validate_readiness_item(item)`, and the CLI-facing validate-readiness command
stable while readiness lanes move to owner modules.

#### Scenario: readiness validator runs after extraction
- **WHEN** callers run `validate_readiness(config)`
- **THEN** it writes the same readiness artifacts, summary schema, release
  blocker shape, redaction behavior, and final `ready` versus
  `release_blocked` semantics for equivalent fixtures

### Requirement: Deterministic evidence never satisfies final live readiness
Readiness extraction SHALL preserve the distinction between deterministic
review evidence and live proof acceptance.

#### Scenario: deterministic summaries pass
- **WHEN** dependency summaries or scheduler review evidence pass their
  deterministic contracts but required live proof receipts are missing
- **THEN** final readiness remains `release_blocked`

#### Scenario: all required live proofs are accepted
- **WHEN** every required live proof item is `passed` with
  `live_proof_accepted=true` and no item is failed, blocked, or release-blocked
- **THEN** final readiness is `ready`

### Requirement: Readiness extraction uses shared item and proof contracts
Readiness extraction SHALL introduce shared item validation, live-proof loading,
artifact writing, dependency binding, scheduler binding, redaction, safe-write,
and bounded JSON contracts before moving proof-specific validators. Each PR SHALL
update `docs/governance/READINESS_VALIDATION_LANE_INVENTORY.md` with the owner
module, retained aggregator surface, removal condition if any, and focused
verification command for the family it moves.

#### Scenario: proof-specific validator is extracted
- **WHEN** auth, alert, rollback, target-env, dependency, or scheduler proof
  validation moves to an owner module
- **THEN** it consumes the shared live-proof loader and item contract rather
  than duplicating receipt parsing, raw alias validation, redaction, or path
  safety

#### Scenario: item contract is invalid
- **WHEN** an extracted lane returns an invalid readiness item
- **THEN** `validate_readiness_item` or the shared item contract reports the
  same validation error namespace as before extraction

#### Scenario: readiness lane inventory is updated with the extraction
- **WHEN** a readiness family changes ownership or retention classification
- **THEN** the readiness lane inventory records the owner module, retained
  aggregator surface, removal condition if any, and focused verification command
  in the same PR

### Requirement: Readiness final aggregation moves last
Readiness final aggregation SHALL remain behind `validate_readiness(config)`
until all lane item/result contracts are stable and parity-tested.

#### Scenario: readiness lane contracts are not stable
- **WHEN** item schemas, release-blocker context rules, artifact references,
  live-proof acceptance, counts, or safe-output parity are not fully covered
- **THEN** final aggregation remains in the existing validator boundary rather
  than moving coupling into a new module

#### Scenario: readiness final aggregation is extracted
- **WHEN** final aggregation moves after all prerequisite owner families are
  stable
- **THEN** `_final_ready` semantics, summary schema, release blocker list,
  counts, artifact refs, safe output behavior, and deterministic-vs-live
  separation remain equivalent
