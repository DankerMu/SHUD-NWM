## MODIFIED Requirements

### Requirement: The surviving `*_invocation` slots MUST name their real truth source

The evidence schema SHALL annotate every surviving `*_invocation` slot
with its real truth source. The five slots of
`timeseries_compression_live_evidence` — `recovery.invocation`,
`migration.first_invocation`, `migration.second_invocation`,
`receipts.dry_run_invocation` and `receipts.enforce_invocation` — SHALL
each carry a schema `description` stating what the slot actually is:
required by the verifier's exact-key check on the input bundle and by
the schema in a v3 qualifying (non-failure) terminal document; its
authored value SHALL be exactly a `{path, sha256, bytes}` artifact
reference (absolute path, lowercase 64-hex `sha256`, non-negative integer
`bytes`), checked by the verifier's input-shape gate inside
`verify_bundle` before that function resolves or uses any artifact
closure, so that a value of any other shape — a mapping with extra or
missing keys, a mapping that wraps a reference, a string, `null` — fails
the run closed and never qualifies. The gate's own rejection names the
slot; when the verifier CLI resolves the artifact closure ahead of
`verify_bundle`, a wrapper around an unavailable reference MAY fail
first at the closure node instead, which is fail-closed all the same.
Its invocation semantics — argv, exit code, timings — are never interpreted;
the terminal slot re-derived from `execution.ledger` rather than copied
from what was authored; and the authored value itself a
closure node, retained in the terminal `source_manifest`. The five
slots and the raw-bytes dereferencer (`_artifact_ref_from_raw`) SHALL
share one definition of "well-formed reference" (one helper, one message
set); the byte-loading and streaming dereferencers keep their own
existing checks and messages unchanged. The runbook narrative
describing these referenced contracts SHALL name the five keys and SHALL
NOT describe them as optional.

The description SHALL NOT claim that the authored value is ignored,
unread, or absent from the terminal document; SHALL NOT claim that the
terminal slot always differs from the authored value; and SHALL NOT
claim that any non-reference shape is tolerated or merely un-checked.
All three are false. Two live bundle shapes exist and an unqualified
claim must hold for both: the legacy hand-assembled shape, whose five
slots name five distinct files, and the committed bundle author's shape
(`scripts/node27_timeseries_compression_bundle_author.py`), whose five
slots are all the ledger reference itself. The input-shape gate SHALL
NOT change the terminal document produced for either shape beyond
`generated_at`.

Keeping the slots is the recorded decision; this requirement governs
what the contract says about them and the shape the verifier accepts,
not whether they exist. The live argv contract
(`_validate_exact_command_argv` / `_concrete_argv`), the
`database_audit_proof` `{"const": false}` pins, and the #1261 ruling
that launcher/interpreter identity is producer-side attestation rather
than a verifier gate all stay untouched, and no launcher/interpreter
identity gate is introduced. The three-key criterion of
`packages/common/evidence_io.artifact_references` is unchanged.

#### Scenario: Every surviving slot is annotated with its truth source

- **WHEN** `schemas/timeseries_compression_live_evidence.schema.json`
  is loaded and every property declared under a `properties` map whose
  name ends in `invocation` is collected
- **THEN** the collected set is exactly the five known slots, and each
  carries a non-empty `description` naming `execution.ledger` as the
  source the verifier re-derives the slot from and stating that any
  shape other than a `{path, sha256, bytes}` reference fails closed

#### Scenario: Authored invocation content is not v3 truth

- **WHEN** a bundle is verified whose `*_invocation` slots point at
  files whose content contradicts the run — a non-zero exit code, a
  wrong timeout, or the same invocation reused for both migration
  steps
- **THEN** verification still qualifies the bundle, because no code
  reads argv, exit codes or timings out of those files

#### Scenario: A well-formed reference is enforced at the artifact closure

- **WHEN** a slot's authored value is exactly a
  `{path, sha256, bytes}` mapping naming a path that is absent, a
  symlink, or whose hash or size disagrees with the file
- **THEN** the run fails closed at the artifact-closure check

#### Scenario: Any non-reference shape fails closed at the input-shape gate

- **WHEN** a slot's authored value is a mapping with an extra scalar
  key, a bare string, or `null` — for any of the five slots
- **THEN** no closure check ever reaches the value: the input-shape gate
  rejects it with an error naming that slot, and the bundle never
  qualifies

- **WHEN** a slot's authored value is a mapping of another shape that
  *wraps* a well-formed `{path, sha256, bytes}` reference, whether the
  wrapped path exists or not
- **THEN** verification fails closed; when the wrapped path exists the
  rejection comes from the input-shape gate naming the slot, and when it
  is absent the run fails closed no later than the artifact-closure
  check

#### Scenario: A well-formed authored reference is retained in the terminal manifest

- **WHEN** a qualifying bundle whose five slots name five distinct
  well-formed artifact references is verified, and its terminal
  document's `source_manifest` is read
- **THEN** the five authored paths appear there with their authored
  `sha256`/`bytes`, distinct from the ledger reference that occupies
  the five slots themselves, and the terminal document is identical to
  the pre-gate output except `generated_at`

- **WHEN** the bundle is instead one the committed bundle author
  produced, whose five slots are all the ledger reference
- **THEN** `source_manifest` carries that reference once rather than
  five times, because the closure deduplicates identical normalized
  paths, and the bundle qualifies unchanged
