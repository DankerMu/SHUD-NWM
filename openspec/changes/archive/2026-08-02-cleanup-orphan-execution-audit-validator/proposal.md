# Remove the aace0913 orphaned false-oracle validators (#1086)

## Why

`scripts/node27_timeseries_compression_live_evidence.py:648-716` defines
`_validate_execution_audit` — a validator for a
`pgaudit+systemd-journal` audit lane. Its only call site was deleted in
`aace0913` (#1069, supervisor-owned execution lane replaced the pgaudit
lane); repo-wide grep shows zero callers. The dead body asserts a trust
boundary ("pgaudit+systemd-journal audit gate is checked") that is NOT
wired — exactly the G6..G14 "measured contract not enforced" failure
class PR #1085 closed. Downstream is currently safe only because
`schemas/timeseries_compression_live_evidence.schema.json:61,:80` pin
`authorization.database_audit_proof` and
`execution.database_audit_proof` to `{"const": false}`; any future
accidental re-call would splice a false oracle straight back into the
main validation path.

Fixture review found the SAME commit orphaned two sibling validators the
issue did not enumerate: `_validate_invocation_record` (`:589-645`,
5 call sites removed by aace0913; raises command-identity/provenance
`EvidenceError`s — squarely the same unwired-trust-boundary class) and
`_artifact_refs_in` (`:2916`, 2 call sites removed). Deleting only the
first would ship a spec claim ("no unwired validators") the module still
violates.

## What Changes

Delete all three aace0913-orphaned functions (scope extension over issue
#1086's single-function enumeration, recorded as a deviation — same root
commit, same failure class, still S-size). Shared helpers they reference
keep their other call sites (verified counts: `_require_mapping` 107,
`_require_exact_keys` 59, `_parse_utc` 60, `_require_list` 23,
`_text_artifact` 3, `EvidenceError` ~275; `INVOCATION_ARGV` keeps
`:283`/`:295` + test callers; `_invocation_execution_identity` keeps its
live test caller at
tests/test_node27_timeseries_compression_live_evidence.py:197 and MUST
stay). No behavior change — all three are unreachable.

Spec: ADDED requirement in `hypertable-compression` (the capability
owning the compression evidence lane) pinning "no unwired trust-boundary
validators".

## Non-goals

- Re-introducing an audit oracle (needs pgaudit on node-27; separate
  issue per #1086).
- Touching the `database_audit_proof` const-pins or any schema.
- Any other cleanup in the file.
