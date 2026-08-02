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

