# Spec Delta: scheduler-registry-refresh

## MODIFIED Requirements

### Requirement: cutover_gate audit blocks MUST pass one shared strict normalizer on every persistence channel

Every persisted `cutover_gate` audit block — CLI summary, runner receipt, and manifest companion receipt — SHALL be produced by the single shared normalizer (`packages/scheduler/registry_audit.py`), which enforces the three-field shape (`mode` ∈ the audited mode set, `declaration_env` str-or-null, `declaration_present` bool — a missing or explicit-null `declaration_present` defaults to `false`; any other present value that is not a boolean rejects) and rejects malformed input with error code `SCHEDULER_REGISTRY_CUTOVER_AUDIT_INVALID`; no channel may silently rewrite a malformed block to `"not_wired"`, and no field may be silently coerced to a different value.

#### Scenario: Manifest channel is fail-closed on malformed audit input

- **WHEN** `publish_scheduler_registry_manifest` is called with a
  `cutover_gate` block that is not a Mapping, has `mode` outside the audited
  mode set, has a non-string non-null `declaration_env`, or has a
  non-boolean non-null `declaration_present`
- **THEN** the publish SHALL raise with error code
  `SCHEDULER_REGISTRY_CUTOVER_AUDIT_INVALID` before the manifest bytes are
  committed
- **AND** the destination manifest SHALL remain absent or unchanged

#### Scenario: Non-boolean declaration_present is rejected, not coerced

- **WHEN** the normalizer receives a `cutover_gate` block whose
  `declaration_present` is present, non-null, and not a boolean (e.g.
  `"no"`, `1`, `0`, `1.0`, or a list — truthy or falsy alike)
- **THEN** normalization SHALL raise with error code
  `SCHEDULER_REGISTRY_CUTOVER_AUDIT_INVALID` instead of truthy-coercing the
  value into the persisted audit block
- **AND** a missing or explicit-null `declaration_present` SHALL still
  normalize to `false` (unchanged default)

#### Scenario: Runner audit block survives to the manifest receipt verbatim

- **WHEN** `publish_all_basin_scheduler_registry` completes with a runner-built
  enforced audit block
- **THEN** the manifest companion receipt SHALL embed `cutover_gate` with
  `mode`, `declaration_env`, and `declaration_present` byte-for-byte equal to
  the producer's block

#### Scenario: Unwired CLI-aggregate callers record not_wired on both channels

- **WHEN** `publish_all_basin_scheduler_registry` is called with
  `cutover_gate=None`
- **THEN** the CLI summary SHALL record the
  `{"mode": "not_wired", "declaration_env": null,
  "declaration_present": false}` fallback
- **AND** the manifest companion receipt SHALL carry the same `not_wired`
  block, because the aggregate entry point normalizes at its boundary before
  delegating (existing behavior; this change adds the pinning assertion)

#### Scenario: Direct manifest-publisher callers keep the key-omitting shape

- **WHEN** `publish_scheduler_registry_manifest` is called directly with
  `cutover_gate=None` (the worker-mirror, require-direct-grid, and
  direct-grid provisioning callers)
- **THEN** the manifest companion receipt SHALL omit the `cutover_gate` key,
  preserving the pre-existing receipt shape for callers that never wire the
  gate
