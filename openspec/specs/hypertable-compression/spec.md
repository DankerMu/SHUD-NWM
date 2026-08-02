# hypertable-compression Specification

## Purpose
TBD - created by archiving change cleanup-orphan-execution-audit-validator. Update Purpose after archive.
## Requirements
### Requirement: The compression live-evidence module MUST NOT retain unwired trust-boundary validators

The compression live-evidence validation module SHALL NOT contain
validator functions asserting a trust boundary that no execution path
enforces: when a validation lane is replaced (as `aace0913` replaced the
pgaudit lane with the supervisor-owned execution lane), every validator
orphaned by that replacement is deleted rather than left implying an
audit gate that is not wired. Attestation fields for unwired lanes stay
schema-pinned to their honest value (`authorization.database_audit_proof`
and `execution.database_audit_proof` const `false`) rather than being
"supported" by unreachable code.

#### Scenario: aace0913 orphan validators removed

- **WHEN** the compression live-evidence module's Python sources are
  scanned for the validators orphaned by the pgaudit-lane retirement
- **THEN** `grep -rn --include="*.py"` for `_validate_execution_audit`,
  `_validate_invocation_record`, and `_artifact_refs_in` each return
  zero hits, and the `database_audit_proof` schema pins under
  `authorization` and `execution` remain `{"const": false}`

### Requirement: The G14 write-privilege probe MUST derive its target from the single recovery-target source and fail closed

The compression supervision plane SHALL embed in every
`has_write_privilege_on_target` probe (supervisor checkpoint SQL and
benchmark activity SQL) only a target that was produced by the shared
fail-closed target validator from the single recovery-target constant;
the validator SHALL raise — before any SQL is built — for any target
that is not a member of the supervised-hypertable whitelist or does not
match a strict `schema.table` identifier form. No probe SQL may embed a
target table as an independent inline literal that can drift from the
pinned recovery target.

#### Scenario: Probe target and recovery target share one source

- **WHEN** the supervisor builds its G14 checkpoint activity SQL and its
  expected decompress argv
- **THEN** both derive the hypertable schema and name from the same
  shared constant, and
  `grep -rn "has_table_privilege(usename" scripts/` returns zero hits

#### Scenario: Non-whitelisted target is refused before SQL exists

- **WHEN** the target validator is invoked with a target outside the
  supervised-hypertable whitelist or with a malformed identifier
- **THEN** it raises an error and no probe SQL is produced

#### Scenario: Switching the target moves the probe with it

- **WHEN** an activity-SQL builder is invoked with
  `met.forcing_station_timeseries`
- **THEN** the emitted SQL probes write privilege on
  `met.forcing_station_timeseries` and no longer references
  `hydro.river_timeseries`

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

