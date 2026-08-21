# hypertable-compression

## ADDED Requirements

### Requirement: The surviving `*_invocation` slots MUST name their real truth source

The evidence schema SHALL annotate every surviving `*_invocation` slot
with its real truth source. The five slots of
`timeseries_compression_live_evidence` — `recovery.invocation`,
`migration.first_invocation`, `migration.second_invocation`,
`receipts.dry_run_invocation` and `receipts.enforce_invocation` — SHALL
each carry a schema `description` stating what the slot actually is:
required by the verifier's exact-key check on the input bundle and by
the schema in a v3 qualifying (non-failure) terminal document; its
invocation semantics — argv, exit code, timings — never interpreted;
the terminal slot re-derived from `execution.ledger` rather than copied
from what was authored; and the authored value closure-checked, and
retained in the terminal `source_manifest`, only when it is exactly a
`{path, sha256, bytes}` mapping. The runbook narrative describing these
referenced contracts SHALL name the five keys and SHALL NOT describe
them as optional.

The description SHALL NOT claim that the authored value is ignored,
unread, or absent from the terminal document; SHALL NOT claim that the
terminal slot always differs from the authored value; and SHALL NOT
claim that existence and hash enforcement applies unconditionally. All
three are false. Two live bundle shapes exist and an unqualified claim
must hold for both: the legacy hand-assembled shape, whose five slots
name five distinct files, and the committed bundle author's shape
(`scripts/node27_timeseries_compression_bundle_author.py`), whose five
slots are all the ledger reference itself.

Keeping the slots is the recorded decision; this requirement governs
what the contract says about them, not whether they exist. The live
argv contract (`_validate_exact_command_argv` / `_concrete_argv`), the
`database_audit_proof` `{"const": false}` pins, and the #1261 ruling
that launcher/interpreter identity is producer-side attestation rather
than a verifier gate all stay untouched, and no launcher/interpreter
identity gate is introduced.

#### Scenario: Every surviving slot is annotated with its truth source

- **WHEN** `schemas/timeseries_compression_live_evidence.schema.json`
  is loaded and every property declared under a `properties` map whose
  name ends in `invocation` is collected
- **THEN** the collected set is exactly the five known slots, and each
  carries a non-empty `description` naming `execution.ledger` as the
  source the verifier re-derives the slot from

#### Scenario: Authored invocation content is not v3 truth

- **WHEN** a bundle is verified whose `*_invocation` slots point at
  files whose content contradicts the run — a non-zero exit code, a
  wrong timeout, or the same invocation reused for both migration
  steps
- **THEN** verification still qualifies the bundle, because no code
  reads argv, exit codes or timings out of those files

#### Scenario: Enforcement applies only to a well-formed artifact reference

- **WHEN** a slot's authored value is exactly a
  `{path, sha256, bytes}` mapping naming a path that is absent, a
  symlink, or whose hash or size disagrees with the file
- **THEN** the run fails closed at the artifact-closure check

- **WHEN** a slot's authored value is any other shape — a mapping with
  an extra key, a string, or `null`
- **THEN** no closure check reaches it and verification can still
  qualify, because the evidence schema is applied to the terminal
  document rather than to the input bundle

#### Scenario: A well-formed authored reference is retained in the terminal manifest

- **WHEN** a qualifying bundle whose five slots name five distinct
  well-formed artifact references is verified, and its terminal
  document's `source_manifest` is read
- **THEN** the five authored paths appear there with their authored
  `sha256`/`bytes`, distinct from the ledger reference that occupies
  the five slots themselves

- **WHEN** the bundle is instead one the committed bundle author
  produced, whose five slots are all the ledger reference
- **THEN** `source_manifest` carries that reference once rather than
  five times, because the closure deduplicates identical normalized
  paths
