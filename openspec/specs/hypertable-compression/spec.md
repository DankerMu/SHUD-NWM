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

