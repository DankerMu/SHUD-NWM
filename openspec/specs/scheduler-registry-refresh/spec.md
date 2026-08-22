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

`_enforce_registry_classification_reconciliation` SHALL apply the id-only constraint set to every receipt whose classification carries `mode="id_only"` (falling back to `outcome="dry_run"` keying when the legacy receipt has no mode field), rejecting any such receipt whose classification violates a constraint that the id-only classify path guarantees by construction, specifically: `removed.total` must be zero, `package_changed.total` and `declared_cutovers.total` must be zero, `declared_retirements.total` must be zero when the bucket is present (a bucket absent from a legacy receipt reads as zero; the id-only path never evaluates removals, so it can never declare a retirement), `generation` must be null when present (a key absent from a legacy receipt reads as null; the id-only path classifies rows that carry no checksums, so the writer records no binding value there), every `refused` entry's reason must be `registry_cutover_declaration_invalid` (the synthetic `__declaration__` marker is the only refusal the writer can attach to an id-only classification; the legacy outcome-keyed fallback keeps rejecting all refused entries as before), a receipt-level cutover refusal `reason` still requires `refused.total >= 1` (writer sets the refusal reason and appends the refused row in the same action — the two must stand or fall together), `previous_registry_sha256` and `previous_model_count` must be null together or non-null together (with a non-boolean integer count >= 0), `new_registry_sha256` must be null (an id-only classification only arises from dry_run, which never publishes a registry), when no previous registry is recorded `unchanged.total` must also be zero (with an empty `previous_by_id` every prospective row classifies as added — the dry_run dual of the bootstrap sum invariant), and when a previous registry exists `unchanged.total` must not exceed `previous_model_count`. The validator SHALL NOT apply the full previous-side equality (`unchanged + package_changed + removed == previous_model_count`) to id-only classifications, because the id-only path never evaluates removals — regardless of the receipt's terminal outcome. On the mode-keyed id-only arm the validator SHALL additionally require the `refused` group to be untruncated with `total == len(items)` and `total <= 1` and every entry's `model_id` equal to the synthetic `__declaration__` marker, and SHALL reject any `outcome="dry_run"` receipt whose refused total is non-zero (a declaration failure always terminates with `outcome="failed"`, so no legal writer emits a dry_run refusal); the legacy no-mode arm keeps rejecting all refused entries unchanged.

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

#### Scenario: id-only constraints follow the mode onto failure outcomes

- **WHEN** an `outcome="failed"` receipt carries an honest id-only
  classification with `mode="id_only"` and a previous registry holding
  models absent from the prospective set
- **THEN** validation SHALL pass without raising — the lenient branch is
  selected by mode, not by the terminal outcome

#### Scenario: Forged id-only refused buckets are rejected

- **WHEN** an id-only (`mode="id_only"`) classification carries a
  `refused` group with empty `items` and a non-zero `total`, or
  `truncated=true`, or more than one entry, or an entry whose `model_id`
  is not `__declaration__`
- **THEN** validation SHALL raise `receipt_classification_invalid`

#### Scenario: dry_run receipts carrying any refusal are rejected

- **WHEN** a receipt has `outcome="dry_run"` and a `refused` group with
  `total >= 1`, even when the single entry is an otherwise well-formed
  synthetic `__declaration__` row
- **THEN** validation SHALL raise `receipt_classification_invalid`
- **AND** an `outcome="failed"` id-only receipt carrying the same single
  well-formed `__declaration__` refusal SHALL keep passing

### Requirement: The runner refresh receipt SHALL persist the normalized cutover_gate audit block whenever the runner constructs the audit block

When a scheduler file-provider refresh run constructs a cutover-gate audit block (registry publish path), the persisted refresh receipt SHALL carry that block, normalized by the shared normalizer, as a top-level optional `cutover_gate` key — so that gated and bypassed runs are distinguishable from the on-disk runner artifact alone; runs that fail before the block is constructed SHALL omit the key entirely (never persist a null placeholder), and the receipt JSON Schema and the runtime receipt validator SHALL both admit exactly the three normalized fields (`mode`, `declaration_env`, `declaration_present`) and reject additional or malformed fields over the same corpus; on any receipt whose `outcome` is `published` or `dry_run`, or whose `reason` is one of the registry-cutover refusal reasons, both the receipt JSON Schema and the runtime receipt validator SHALL additionally require the `cutover_gate` key to be present, rejecting its absence with the distinct runtime reason `receipt_cutover_gate_required`, while receipts from runs that fail before the block is constructed remain valid without the key.

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

#### Scenario: Gated outcomes missing the audit block are rejected by both validators

- **WHEN** a receipt with `outcome="published"` or `outcome="dry_run"`
  carries no `cutover_gate` key and is validated against the receipt
  JSON Schema and the runtime receipt validator
- **THEN** the schema validation SHALL fail
- **AND** the runtime validator SHALL raise
  `receipt_cutover_gate_required`

#### Scenario: Registry-cutover refusal receipts missing the audit block are rejected

- **WHEN** a receipt whose `reason` is `registry_cutover_undeclared`,
  `registry_cutover_removal_refused`, or
  `registry_cutover_declaration_invalid` carries no `cutover_gate` key
- **THEN** both the schema and the runtime validator SHALL reject it,
  the runtime side with `receipt_cutover_gate_required`

#### Scenario: Early-failure receipts remain valid without the key and the upgrade path is documented

- **WHEN** a receipt from a run that failed before audit-block
  construction (for example lock contention) omits `cutover_gate`, or an
  operator upgrades a node whose `latest.json` is a pre-#1132 published
  receipt without the key
- **THEN** the early-failure receipt SHALL pass both validators
- **AND** the runbook SHALL document that the legacy published receipt
  now fails `validate_current_receipt` (the install `--enable`
  validation step) and that one manual refresh rewriting `latest.json`
  clears it, the refresh write path itself being unblocked

### Requirement: CLI registry-publish failure diagnostics SHALL carry a normalizer-produced cutover_gate block

When the registry-publish CLI exits non-zero on a publish, discovery, or provider error, the JSON payload written to stderr SHALL embed a `cutover_gate` block produced by the shared normalizer rather than an inline literal, so a failed run leaves the same audited three-field fact a successful summary would.

#### Scenario: Failure payload routes through the shared normalizer

- **WHEN** the CLI's publish call raises and the stderr error payload is
  emitted
- **THEN** the payload's `cutover_gate` SHALL be the shared normalizer's
  output for the CLI-constructed audit block

### Requirement: The classification mode SHALL be persisted and reconciliation SHALL be selected by mode so dry_run failure receipts land on disk with their true reason

`_classify_registry` SHALL record its classification mode (`id_only` on the dry_run path, `full` otherwise) on the classification it produces, `to_receipt()` SHALL persist it as an optional `mode` field admitted by both the receipt JSON Schema and the runtime key-set validator (value restricted to `id_only`/`full`; absence remains valid for legacy receipts), and the reconciliation validator SHALL select its branch by that mode when present — so a dry_run refresh that fails after the precommit gate produces a persisted `outcome="failed"` receipt carrying its true failure reason and its id-only classification, instead of masking the reason behind `primary_receipt_failed` and dropping the receipt entirely.

#### Scenario: dry_run failure after the gate persists the true reason

- **WHEN** a dry_run refresh with a previous canonical registry containing
  a model absent from the prospective set fails after the precommit gate
  (for example a readiness derivation error)
- **THEN** `refresh_scheduler_file_providers` SHALL NOT raise
  `primary_receipt_failed`; the receipt SHALL persist to both the history
  and latest channels (no newer receipt present) with `outcome="failed"`,
  the injected true reason, and the id-only classification retained with
  `mode="id_only"`

#### Scenario: An id-only classification may carry the declaration-invalid refusal marker

- **WHEN** an `outcome="failed"` receipt carries `mode="id_only"` with
  `reason="registry_cutover_declaration_invalid"` and refused entries
  whose reason is `registry_cutover_declaration_invalid` (the synthetic
  `__declaration__` marker the writer appends after classification,
  dry_run included)
- **THEN** validation SHALL pass; an id-only `refused` entry with any
  other reason SHALL raise `receipt_classification_invalid`
- **AND** an id-only receipt whose `reason` is a cutover refusal reason
  but whose `refused.total` is zero SHALL raise
  `receipt_classification_invalid`

#### Scenario: Forged mode/outcome combinations are rejected

- **WHEN** a receipt carries `outcome="dry_run"` with
  `classification.mode="full"`, or `outcome="published"` with
  `classification.mode="id_only"`, or `outcome="published_receipt_failed"`
  with `classification.mode="id_only"` (that outcome only arises from a
  committed real publish, always classified in full mode — and it is the
  one shape the emergency-reconstruct channel republishes), or a `mode`
  outside `id_only`/`full`
- **THEN** validation SHALL raise `receipt_classification_invalid`

#### Scenario: Legacy mode-less receipts keep their current behavior

- **WHEN** a persisted receipt without a `classification.mode` field is
  read back through receipt validation
- **THEN** the reconciliation branch SHALL fall back to the
  `outcome="dry_run"` keying exactly as before this change — a mode-less
  id-only-shaped classification on an `outcome="failed"` receipt is still
  rejected, and a mode-less dry_run receipt still takes the lenient branch

#### Scenario: Full-mode tamper resistance is not weakened

- **WHEN** a receipt carries `classification.mode="full"` with tampered
  `unchanged`/`removed` totals violating the previous-side equality
- **THEN** validation SHALL raise `receipt_classification_invalid` exactly
  as the outcome-keyed branch does today

### Requirement: Manual publisher concurrency SHALL be operator-gated and the CLI SHALL warn about the refresh-timer prohibition on startup

The manual publisher CLI's concurrency with the provider-refresh timer is governed by an explicit operator prohibition (runbook), not by an `expected_preimage` CAS — the CLI SHALL print a startup WARNING line to stderr, unconditional for every run that reaches argument-validated startup (argparse usage errors, exit 2, are out of scope), naming the refresh timer unit and directing the operator to confirm the timer AND its oneshot service are not active; the warning SHALL NOT alter exit codes or corrupt the machine-readable stderr JSON payload (which remains parseable from the final stderr line), and the capability's governing documents — the `scheduler-registry-refresh` design/spec/tasks text and `docs/runbooks/current-production-ops.md` (§3.1.2 plus the manual-publisher entry) — SHALL NOT claim CAS protection for the manual-publisher path while `main()` does not populate `expected_preimage`.

#### Scenario: Startup warning is present on success and failure runs

- **WHEN** the manual publisher CLI runs to a successful publish, or exits
  non-zero on a publish/discovery/provider error
- **THEN** stderr SHALL contain the startup WARNING line naming
  `nhms-scheduler-file-provider-refresh.timer`
- **AND** on the failure run the existing JSON error payload SHALL still
  parse from the final stderr line with unchanged fields

#### Scenario: Design and runbook state the factual gating boundary

- **WHEN** an operator reads the D7#7 concurrency invariant or the
  runbook's manual-publisher section
- **THEN** both SHALL state that manual-publisher concurrency is
  operator-gated (explicit timer prohibition with a status-check command)
  and that the CAS parameter is exercised only by the internal refresh
  runner

### Requirement: Canonical registry removals SHALL be admissible only through a declared retirement entry, with undeclared removals remaining fail-closed

The registry cutover declaration SHALL support a `transition_mode: "retire"` entry — `old_checksum` equal to the removed model's previous canonical `package_checksum` and `new_checksum` explicitly `null` — as the only channel through which a model row may legally leave the canonical registry during a refresh. The gate SHALL classify removals exactly as before (a removal is any previous-canonical model id absent from the prospective registry, whatever the cause), record removals matched by a valid retirement entry — under a declaration that is valid as a whole — in a `declared_retirements` classification bucket (a subset of `removed`) without a refusal, and keep every removal not so admitted refused: with exactly one refusal row attributable to the removal itself — `registry_cutover_removal_refused` when no retirement entry names the model, or `registry_cutover_declaration_invalid` when a named retirement entry fails checksum validation or the declaration as a whole is invalid — while the unknown-entry rule may additionally refuse the declaration entry itself (a replace entry naming a model absent from the prospective registry yields both that entry's `registry_cutover_declaration_invalid` row and the removal's `registry_cutover_removal_refused` row). Whether a matched retirement is admitted SHALL NOT depend on the iteration order of the removal set: a declaration invalidated by any entry admits no retirement in that run. Retirement entries SHALL inherit the declaration's existing generation binding, effective-cycle alignment, expiry window, and byte-cap constraints without any retire-specific bypass. Because that binding is what an operator must reproduce, a classification produced by the full path SHALL record the prospective generation it bound to on the receipt, so a refusal receipt carries every value the declaration needs; an id-only classification SHALL record no generation, its rows carrying no checksums to derive the binding value a real publish would use. Existing replace-only declaration files SHALL remain valid unchanged, and a reader without retirement support SHALL fail closed on a retirement declaration with `registry_cutover_declaration_invalid`.

#### Scenario: Declared retirement admits the removal and the refresh publishes

- **WHEN** a previously canonical model is absent from the prospective registry (for example its package turned invalid and bulk publish legally skipped it) and the cutover declaration carries a `retire` entry whose `model_id` matches, whose `old_checksum` equals that model's previous canonical `package_checksum`, whose `new_checksum` is `null`, and whose declaration binds to the prospective generation
- **THEN** the precommit gate SHALL NOT refuse for that removal: the refresh publishes, the canonical registry loses exactly that row, the classification receipt records the model in `declared_retirements`, and the remaining healthy models publish normally

#### Scenario: Undeclared removals keep failing closed

- **WHEN** a previously canonical model is absent from the prospective registry and no valid retirement entry matches it — no declaration, a declaration for other models only, a retirement entry whose `old_checksum` does not equal the previous canonical `package_checksum`, or a declaration bound to a different generation
- **THEN** the gate SHALL refuse with `registry_cutover_removal_refused` (checksum and generation mismatches surface as `registry_cutover_declaration_invalid` per the existing priority ladder), the canonical registry SHALL remain byte-identical, and nothing SHALL publish in that run

#### Scenario: Retirement entries are validated against the removal set, not the prospective registry

- **WHEN** a declaration carries a `retire` entry for a model that is still present in the prospective registry, or for a model that was never in the previous canonical registry
- **THEN** the gate SHALL refuse with `registry_cutover_declaration_invalid` — a retirement can only name a model that is actually leaving the canonical registry in this refresh

#### Scenario: Removal refusals carry the skip-cause evidence when the model was skipped rather than deleted

- **WHEN** a removal refused with `registry_cutover_removal_refused` corresponds to a model that bulk publish discovered but skipped as unpublishable
- **THEN** that `registry_cutover_removal_refused` entry SHALL carry the model's inventory `status`, `missing_required_files`, and `invalid_required_files` (the same keys the publisher's not-publishable diagnostics use), and a removal whose model directory disappeared entirely SHALL omit those keys — so operators can tell an invalid-package skip from a deleted directory (declaration-invalid refusal rows carry the entry evidence instead and are out of this scenario's scope)

#### Scenario: Retirement-aware reconciliation stays tamper-evident

- **WHEN** a classification receipt carries a `declared_retirements` bucket
- **THEN** reconciliation SHALL reject the receipt with `receipt_classification_invalid` unless the bucket's model ids are a subset of `removed` AND `declared_retirements.total` does not exceed `removed.total` AND every truncated classification group carries a full item list (`truncated` true requires exactly the item cap, which is the shape the writer produces by construction — without it a forger could empty the items, inflate the total, and deflate a lower bound computed from totals; above the cap the rows the receipt cannot name remain unverifiable by item, a residue this rule bounds rather than removes), the previous-side equality `unchanged + package_changed + removed == previous_model_count` still holds with removals counting declared retirements, and the refused lower bound accounts for declared retirements as non-refused removals — deducting the retirements the bucket actually names while the bucket is untruncated, and its total once truncation makes naming impossible; a legacy receipt without the bucket SHALL keep validating with the bucket read as empty

### Requirement: The provider snapshot read SHALL reject a replacement that restores the destination's metadata

`read_provider_snapshot` SHALL bind its returned bytes to one stable physical
preimage and SHALL raise `provider_preimage_changed` (phase `precommit`) when
the destination is replaced between the preimage capture and the payload read,
**including when the replacement is subsequently reverted so that every captured
metadata field — device, inode, mode, uid, gid, size, and `mtime_ns` — is
identical before and after**. The content-digest comparison against the
captured preimage SHALL be the guard that holds in that case, and its coverage
SHALL NOT depend on filesystem timestamp granularity: the covering test SHALL
fail if that comparison is removed, on both a nanosecond-timestamp filesystem
(APFS) and a coarse-tick filesystem (ext4 at 4 ms).

#### Scenario: Content replaced during the read and metadata restored before the second capture

- **WHEN** the destination holds `generation-a`, is replaced with an
  equal-length `generation-b` after the preimage capture but before the payload
  read, and is then restored to `generation-a` with its original `mtime_ns`
  reapplied before the second capture
- **THEN** the two captured preimages compare equal
- **AND** `read_provider_snapshot` raises `ProviderAtomicError` with reason
  `provider_preimage_changed`
- **AND** the raise is produced by the content-digest comparison alone, such
  that removing that comparison makes the read succeed

#### Scenario: Content replaced during the read with a different length and not restored

- **WHEN** the destination holds `generation-a` and is replaced with a
  different-length payload after the preimage capture but before the payload
  read, with no restoration
- **THEN** the two captured preimages differ on `size`
- **AND** `read_provider_snapshot` raises `ProviderAtomicError` with reason
  `provider_preimage_changed`
- **AND** the divergence is observable independently of `mtime_ns` granularity

