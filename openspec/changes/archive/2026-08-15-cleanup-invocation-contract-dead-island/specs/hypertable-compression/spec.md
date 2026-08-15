# Delta: hypertable-compression（INVOCATION_ARGV 死岛删除）

## ADDED Requirements

### Requirement: The invocation-contract dead island MUST be removed rather than left as a drifted pseudo-oracle

The compression live-evidence module SHALL NOT carry the
production-dead invocation-contract island orphaned by the
supervisor-owned execution lane (#1069) and exposed by the aace0913
orphan-validator removal (#1239): the `INVOCATION_ARGV` mapping (a
second, already-drifted hand copy of the launch contract whose live
single source is the supervisor-ledger lane's `command["argv"]`
equality gates), the `_TIMEOUT_PREFIX` launcher-wall constant, and the
`_invocation_execution_identity` resolver. Test fixtures SHALL stop
stamping unverified provenance fields (argv, launcher identity,
resolved paths, artifact bindings) into invocation artifacts whose
content the verifier never parses, so no bundle artifact looks like a
provenance oracle that is in fact constrained only as
`{path, sha256, bytes}`. The live argv contract
(`_validate_exact_command_argv` / `_concrete_argv`) and the
`database_audit_proof` schema pins stay untouched, and the
content-is-not-truth sentinel test
(`test_legacy_authored_invocations_do_not_contribute_to_v3_truth`)
survives with its negative semantics intact.

#### Scenario: The island symbols are gone

- **WHEN** the repository's Python sources are scanned
- **THEN** `grep -rn --include="*.py"` for `INVOCATION_ARGV`,
  `_invocation_execution_identity`, and `_TIMEOUT_PREFIX` each return
  zero hits

#### Scenario: No unverified provenance fields remain in any fixture

- **WHEN** the repository's Python sources are scanned for the
  pseudo-provenance field names as whole words
  (`grep -rnE "\b(launcher_argv|resolved_interpreter|resolved_wrapper|resolved_env_file|resolved_repo_path|resolved_script|artifact_bindings)\b" --include="*.py"`
  — word-bounded because the bare substring `resolved_script` collides
  with the unrelated live CI-selection helper
  `_resolved_script_modules`, which stays untouched)
- **THEN** each returns zero hits, and the invocation fixtures carry
  no field asserting launcher provenance or artifact bindings

#### Scenario: The live contract and honest pins are untouched

- **WHEN** the deletion diff is inspected
- **THEN** `_validate_exact_command_argv` and `_concrete_argv` are
  unchanged, the `authorization.database_audit_proof` and
  `execution.database_audit_proof` schema pins remain
  `{"const": false}`, and the legacy-invocation sentinel test still
  passes with its `qualifies_task_4_5 is True` assertion intact
