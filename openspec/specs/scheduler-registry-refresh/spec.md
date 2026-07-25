# scheduler-registry-refresh Specification

## Purpose
TBD - created by archiving change unify-cutover-gate-audit-normalizer. Update Purpose after archive.
## Requirements
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

### Requirement: Bootstrap classification receipts SHALL satisfy the empty-previous sum invariant

When a registry classification receipt with a non-`dry_run` outcome carries `previous_registry_sha256 = null` (bootstrap semantics: no previous canonical registry existed), the reconciliation validator SHALL reject the receipt with `receipt_classification_invalid` unless `unchanged.total + package_changed.total + removed.total == 0` — the dual of the non-bootstrap equality `unchanged + package_changed + removed == previous_model_count` — so a tampered on-disk bootstrap receipt cannot smuggle non-empty carry-over buckets past validation.

#### Scenario: Tampered bootstrap receipt with non-empty buckets is rejected

- **WHEN** a receipt with `previous_registry_sha256 = null` and
  `previous_model_count = null` carries a non-zero total in any of
  `removed`, `unchanged`, or `package_changed`, and the receipt `outcome`
  is not `dry_run` (the dry_run branch runs id-only reconciliation and
  returns before the previous-registry branch — its reconciliation gap is
  tracked separately, out of scope here)
- **THEN** receipt validation SHALL raise with
  `receipt_classification_invalid`, even when the
  `added + unchanged + package_changed == prospective_model_count` equality
  and the refused lower bound are satisfied by symmetric forgery

#### Scenario: Honest bootstrap receipt keeps validating

- **WHEN** a bootstrap receipt carries empty `removed`, `unchanged`, and
  `package_changed` buckets (the only shape `_classify_registry` can
  construct when no previous registry exists)
- **THEN** receipt validation SHALL pass unchanged

### Requirement: The refresh wrapper SHALL admit the cutover declaration path as an optional environment key without weakening its parse constraints

The systemd refresh wrapper's EnvironmentFile allowlist SHALL accept `NHMS_REGISTRY_CUTOVER_DECLARATION_PATH` as an optional (non-required) key and export it to the runner process, so the systemd path can execute declared package cutovers; the key's absence SHALL leave wrapper behavior unchanged (runner refuses undeclared cutovers), and every other wrapper safety constraint (0600 mode, symlink refusal, DB-selector refusal, newline and duplicate refusal, required-key set, direct-grid assertion) SHALL remain in force.

#### Scenario: EnvironmentFile carrying a declaration path passes the wrapper

- **WHEN** the mode-0600 EnvironmentFile contains
  `NHMS_REGISTRY_CUTOVER_DECLARATION_PATH=<absolute path>` alongside the
  required refresh keys
- **THEN** the wrapper SHALL parse successfully and the exec'd runner process
  SHALL observe the variable with the exact value, under the same name the
  runner reads (`CUTOVER_DECLARATION_ENV`)

#### Scenario: Absent declaration key keeps the safe-refuse default

- **WHEN** the EnvironmentFile omits `NHMS_REGISTRY_CUTOVER_DECLARATION_PATH`
- **THEN** the wrapper SHALL behave exactly as before this change (exit 0
  with required keys present, variable unset in the runner process), leaving
  the cutover gate to refuse undeclared package cutovers

#### Scenario: No other parse constraint is relaxed

- **WHEN** the EnvironmentFile contains a key outside the allowlist, a DB
  selector, a duplicate key, or a value with a newline
- **THEN** the wrapper SHALL still fail fast exactly as before this change

### Requirement: The receipt validator SHALL bind dry_run receipts to the reconciliation constraints that hold in id-only mode

`_enforce_registry_classification_reconciliation` SHALL reject an
`outcome="dry_run"` receipt whose classification violates a constraint
that the id-only classify path guarantees by construction, specifically:
`removed.total` must be zero, `previous_registry_sha256` and
`previous_model_count` must be null together or non-null together (with a
non-boolean integer count >= 0), `new_registry_sha256` must be null (a
dry_run never publishes a registry), when no previous registry is recorded
`unchanged.total` must also be zero (with an empty `previous_by_id` every
prospective row classifies as added — the dry_run dual of the bootstrap
sum invariant), and when a previous registry exists `unchanged.total` must
not exceed `previous_model_count`. The validator
SHALL NOT apply the full previous-side equality (`unchanged +
package_changed + removed == previous_model_count`) to dry_run receipts,
because dry_run classification never evaluates removals.

#### Scenario: Tampered dry_run receipt with removed entries is rejected

- **WHEN** a receipt has `outcome="dry_run"` and
  `classification.removed.total != 0`, whether or not a previous registry
  is recorded
- **THEN** validation SHALL raise `receipt_classification_invalid`

#### Scenario: Contradictory previous-registry shape is rejected in dry_run

- **WHEN** a dry_run receipt carries `previous_registry_sha256 = null`
  together with a non-null `previous_model_count` (the pairing
  contradiction — the newly reachable discriminator at this validator;
  boolean/negative/non-integer counts are additionally rejected by the
  branch-local guard, mirroring the field-level validation that already
  runs on the receipt path)
- **THEN** validation SHALL raise `receipt_classification_invalid`

#### Scenario: Forged new-registry sha on a dry_run receipt is rejected

- **WHEN** a dry_run receipt carries a non-null `new_registry_sha256`
- **THEN** validation SHALL raise `receipt_classification_invalid`

#### Scenario: Bootstrap dry_run receipt with unchanged entries is rejected

- **WHEN** a dry_run receipt records no previous registry
  (`previous_registry_sha256 = null`) but a non-zero `unchanged.total`
- **THEN** validation SHALL raise `receipt_classification_invalid`

#### Scenario: dry_run constraints are enforced on the receipt validation path

- **WHEN** a tampered dry_run receipt (carrying the full provider triple)
  violating any constraint above is passed through receipt validation
- **THEN** the receipt SHALL be rejected with
  `receipt_classification_invalid` — the reconciliation enforcement call
  covers `outcome="dry_run"` receipts, not only real-publish outcomes

#### Scenario: dry_run unchanged total exceeding the previous count is rejected

- **WHEN** a dry_run receipt records a previous registry with
  `previous_model_count = N` and `classification.unchanged.total > N`
- **THEN** validation SHALL raise `receipt_classification_invalid`

#### Scenario: Honest dry_run receipts keep passing

- **WHEN** a dry_run receipt matches the writer's id-only construction —
  zero removed/package_changed/refused/declared totals,
  `added + unchanged == prospective_model_count`, paired previous fields,
  `unchanged <= previous_model_count`, null `new_registry_sha256` —
  including the legal shape where the
  previous registry holds models absent from the prospective set (so the
  previous-side sum equality does not hold)
- **THEN** validation SHALL pass without raising

### Requirement: The runner refresh receipt SHALL persist the normalized cutover_gate audit block whenever the runner constructs the audit block

When a scheduler file-provider refresh run constructs a cutover-gate audit block (registry publish path), the persisted refresh receipt SHALL carry that block, normalized by the shared normalizer, as a top-level optional `cutover_gate` key — so that gated and bypassed runs are distinguishable from the on-disk runner artifact alone; runs that fail before the block is constructed SHALL omit the key entirely (never persist a null placeholder), and the receipt JSON Schema and the runtime receipt validator SHALL both admit exactly the three normalized fields (`mode`, `declaration_env`, `declaration_present`) and reject additional or malformed fields over the same corpus.

#### Scenario: Registry-publish refresh persists the audit block

- **WHEN** a refresh run publishes the registry with the cutover gate
  enforced
- **THEN** the persisted refresh receipt SHALL contain a top-level
  `cutover_gate` object equal to the shared normalizer's output for the
  runner's audit block, including the observed `declaration_present`
  boolean (both the declaration-present and declaration-absent runs are
  representable and distinguishable)

#### Scenario: Runs failing before block construction omit the key

- **WHEN** a refresh run fails before the audit block is constructed
  (for example lock contention or a provider-preimage mismatch)
- **THEN** the persisted refresh receipt SHALL NOT contain a
  `cutover_gate` key

#### Scenario: Schema and runtime validator reject the same malformed blocks

- **WHEN** a refresh receipt carrying a `cutover_gate` block with an
  extra fourth field or a mode outside the audited mode set is validated
  against the receipt JSON Schema, or read back from disk through the
  runtime receipt validator
- **THEN** both validations SHALL fail

### Requirement: CLI registry-publish failure diagnostics SHALL carry a normalizer-produced cutover_gate block

When the registry-publish CLI exits non-zero on a publish, discovery, or provider error, the JSON payload written to stderr SHALL embed a `cutover_gate` block produced by the shared normalizer rather than an inline literal, so a failed run leaves the same audited three-field fact a successful summary would.

#### Scenario: Failure payload routes through the shared normalizer

- **WHEN** the CLI's publish call raises and the stderr error payload is
  emitted
- **THEN** the payload's `cutover_gate` SHALL be the shared normalizer's
  output for the CLI-constructed audit block

