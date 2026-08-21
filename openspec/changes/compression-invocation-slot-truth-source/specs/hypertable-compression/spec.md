# hypertable-compression

## ADDED Requirements

### Requirement: The surviving `*_invocation` slots MUST name their real truth source

The evidence schema SHALL annotate every surviving `*_invocation` slot
with its real truth source. The five v3-required slots of
`timeseries_compression_live_evidence` — `recovery.invocation`,
`migration.first_invocation`, `migration.second_invocation`,
`receipts.dry_run_invocation` and `receipts.enforce_invocation` —
SHALL each carry a schema `description` stating what the slot actually
is: required, and enforced only as an artifact-closure node (the file
must exist as a regular non-symlink whose `sha256`/`bytes` match, and
if it parses as JSON it is complexity-bounded and its own nested
artifact references are resolved transitively), with its authored
`path`/`sha256`/`bytes` retained in the terminal `source_manifest`;
the invocation semantics inside it — argv, exit code, timings — never
interpreted; and the slot in the terminal document never the authored
reference, because the verifier re-derives it from `execution.ledger`.
The runbook narrative describing these referenced contracts SHALL name
the five keys and SHALL NOT describe them as optional.

The description SHALL NOT claim that the authored value is ignored,
unread, or absent from the terminal document. All three are false: the
closure reads and hashes the file, parses it when it is JSON, and
retains its reference in `source_manifest`. What the verifier never
interprets is the invocation semantics, and what it overwrites is the
slot.

Keeping the slots is the recorded decision; this requirement governs
what the contract says about them, not whether they exist. The live
argv contract (`_validate_exact_command_argv` / `_concrete_argv`), the
`database_audit_proof` `{"const": false}` pins, and the #1261 ruling
that launcher/interpreter identity is producer-side attestation rather
than a verifier gate all stay untouched, and no launcher/interpreter
identity gate is introduced.

#### Scenario: Every surviving slot is annotated with its truth source

- **WHEN** `schemas/timeseries_compression_live_evidence.schema.json`
  is loaded and the five `*_invocation` property objects are read
- **THEN** each carries a non-empty `description` naming
  `execution.ledger` as the source the verifier re-derives the
  reference from

#### Scenario: Authored content is still not v3 truth

- **WHEN** a bundle is verified whose five `*_invocation` slots point
  at files whose content contradicts the run (a non-zero exit code, a
  wrong timeout, the same invocation reused for both migration steps)
- **THEN** verification still qualifies the bundle, and all five slots
  in the terminal document are identical to `execution.ledger` — the
  annotation describes the behavior that the existing
  `test_legacy_authored_invocations_do_not_contribute_to_v3_truth`
  sentinel already pins

#### Scenario: The annotation does not overclaim that the value is inert

- **WHEN** a slot names a path that is absent, a symlink, or whose
  `sha256`/`bytes` disagree with the file
- **THEN** the run fails closed at the artifact-closure check

#### Scenario: The authored reference survives in the terminal manifest

- **WHEN** a qualifying bundle is verified and its terminal document's
  `source_manifest` is read
- **THEN** the five authored `*_invocation` paths appear there with
  their authored `sha256`/`bytes`, distinct from the ledger reference
  that occupies the five slots themselves
