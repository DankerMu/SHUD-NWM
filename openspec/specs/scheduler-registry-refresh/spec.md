# scheduler-registry-refresh Specification

## Purpose
TBD - created by archiving change unify-cutover-gate-audit-normalizer. Update Purpose after archive.
## Requirements
### Requirement: cutover_gate audit blocks MUST pass one shared strict normalizer on every persistence channel

Every persisted `cutover_gate` audit block — CLI summary, runner receipt, and manifest companion receipt — SHALL be produced by the single shared normalizer (`packages/scheduler/registry_audit.py`), which enforces the three-field shape (`mode` ∈ the audited mode set, `declaration_env` str-or-null, `declaration_present` bool) and rejects malformed input with error code `SCHEDULER_REGISTRY_CUTOVER_AUDIT_INVALID`; no channel may silently rewrite a malformed block to `"not_wired"`.

#### Scenario: Manifest channel is fail-closed on malformed audit input

- **WHEN** `publish_scheduler_registry_manifest` is called with a
  `cutover_gate` block that is not a Mapping, has `mode` outside the audited
  mode set, or has a non-string non-null `declaration_env`
- **THEN** the publish SHALL raise with error code
  `SCHEDULER_REGISTRY_CUTOVER_AUDIT_INVALID` before the manifest bytes are
  committed
- **AND** the destination manifest SHALL remain absent or unchanged

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

### Requirement: Registry identity-field equality SHALL treat symmetric absence as identical and any asymmetric absence as drift

The registry classification identity predicate (`_rows_have_identical_identity`) SHALL classify two rows as identical when every identity field is equal on both sides — including both sides `None` (flat fields) and both sides missing (nested paths, sentinel-equal) — and SHALL classify them as differing when any identity field is asymmetric, including falsy-vs-None flat values (`0` vs `None`, `""` vs `None`) and missing-key vs explicit-null nested values; these semantics are pinned by direct regression tests.

#### Scenario: Symmetric absence stays unchanged

- **WHEN** two registry rows agree on every identity field, with a flat
  identity field `None` on both sides, the nested
  `resource_profile.source_inventory_checksum` `None` on both sides, or the
  top-level `resource_profile` key absent on both sides
- **THEN** the identity predicate SHALL return `True` (row classifies as
  `unchanged`, not `package_changed`)
- **AND** a flat identity field missing on one side and explicitly `None` on
  the other SHALL also return `True` (flat comparison collapses missing and
  null via `dict.get()`; a sentinel-based rewrite flipping this is a
  behavior change this requirement guards against)

#### Scenario: Asymmetric falsy values are drift, not identity

- **WHEN** a flat identity field is `None` on one side and a falsy non-None
  value (`0` for `segment_count`, `""` for `lifecycle_state`) on the other
- **THEN** the identity predicate SHALL return `False` — a truthiness-based
  comparison that conflates falsy values with `None` violates this
  requirement

#### Scenario: Missing nested key differs from explicit null

- **WHEN** one row lacks the top-level `resource_profile` key entirely and
  the other carries `resource_profile.source_inventory_checksum` with an
  explicit `null` value
- **THEN** the identity predicate SHALL return `False` (missing-key sentinel
  and JSON null are materially different identity facts)

### Requirement: The lenient receipt-order reader SHALL fail safe to None on any malformed payload so a corrupted latest.json never bricks the next publish

`_lenient_receipt_order` SHALL return `None` — never raise — for any payload that is not a Mapping, lacks a non-empty string `run_id`, or carries a missing or unparsable `started_at`; for a valid payload it SHALL return a `(started_at, run_id)` tuple with a timezone-aware datetime; and `_publish_primary_receipt` SHALL treat a `None` order (including one caused by undecodable or non-JSON `latest.json` bytes) as `replace_latest = True`, publishing the new receipt successfully. These semantics are pinned by direct regression tests.

#### Scenario: Malformed payload shapes return None, not an exception

- **WHEN** `_lenient_receipt_order` is called with a non-Mapping payload, a
  Mapping whose `run_id` is missing, empty, or not a string, or a Mapping
  whose `started_at` is missing or unparsable
- **THEN** it SHALL return `None` without raising

#### Scenario: Valid payload yields a timezone-aware order tuple

- **WHEN** `_lenient_receipt_order` is called with a Mapping carrying a
  non-empty string `run_id` and an ISO-8601 `started_at` with timezone
- **THEN** it SHALL return `(started_at, run_id)` with the datetime
  timezone-aware (normalized to UTC)

#### Scenario: Corrupted latest.json does not brick the next publish

- **WHEN** `latest.json` on disk contains undecodable or non-JSON bytes and
  `_publish_primary_receipt` is called with a valid new receipt
- **THEN** the publish SHALL succeed and `latest.json` SHALL contain the new
  receipt's canonical bytes

