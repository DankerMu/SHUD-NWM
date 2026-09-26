# scheduler-registry-refresh Specification

## Purpose

The node-22 file-provider refresh lane republishes, database-free, the scheduler registry, its worker mirror, the readiness index and the state index. This spec is the contract for that lane:

- the refresh runner's provider transactions and rollbacks, and the receipts it writes (primary, emergency and history), including what their `after_*` evidence may claim;
- the cutover-gate audit and the classification modes;
- retirement declarations;
- provider snapshot reads;
- the systemd installers that arm and roll back the lane and its independent liveness probe, with their failure paths and restore baselines;
- the health probe that grades the lane's timer and manifest age before the consumers' 168-hour freshness bound expires.

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

A removal refusal attributable to a model discovered but skipped as unpublishable SHALL mirror the inventory cause set through the publisher, refresh runtime validator, bounded receipt projection, and JSON Schema. The optional list-valued keys are `missing_required_files`, `invalid_required_files`, and `unreadable_required_files`, use the existing receipt collection/string bounds, and remain optional so historical receipts without the additive unreadable key still validate and reconstruct unchanged. This evidence-only extension SHALL NOT change which models are publishable or alter consumers that intentionally compare only the `missing_required_files` set.

#### Scenario: Declared retirement admits the removal and the refresh publishes

- **WHEN** a previously canonical model is absent from the prospective registry (for example its package turned invalid and bulk publish legally skipped it) and the cutover declaration carries a `retire` entry whose `model_id` matches, whose `old_checksum` equals that model's previous canonical `package_checksum`, whose `new_checksum` is `null`, and whose declaration binds to the prospective generation
- **THEN** the precommit gate SHALL NOT refuse for that removal: the refresh publishes, the canonical registry loses exactly that row, the classification receipt records the model in `declared_retirements`, and the remaining healthy models publish normally

#### Scenario: Undeclared removals keep failing closed

- **WHEN** a previously canonical model is absent from the prospective registry and no valid retirement entry matches it — no declaration, a declaration for other models only, a retirement entry whose `old_checksum` does not equal the previous canonical `package_checksum`, or a declaration bound to a different generation
- **THEN** the gate SHALL refuse with `registry_cutover_removal_refused` (checksum and generation mismatches surface as `registry_cutover_declaration_invalid` per the existing priority ladder), the canonical registry SHALL remain byte-identical, and nothing SHALL publish in that run

#### Scenario: Retirement entries are validated against the removal set, not the prospective registry

- **WHEN** a declaration carries a `retire` entry for a model that is still present in the prospective registry, or for a model that was never in the previous canonical registry
- **THEN** the gate SHALL refuse with `registry_cutover_declaration_invalid` — a retirement can only name a model that is actually leaving the canonical registry in this refresh

#### Scenario: Removal refusals carry every skip-cause list when the model was skipped rather than deleted

- **WHEN** a removal refused with `registry_cutover_removal_refused` corresponds to a model that bulk publish discovered but skipped as unpublishable
- **THEN** that `registry_cutover_removal_refused` entry SHALL carry the model's inventory `status`, `missing_required_files`, `invalid_required_files`, and `unreadable_required_files` through the same bounded machine-readable contract used by publisher diagnostics
- **AND** a model whose required file matched but could not be read SHALL name that file under `unreadable_required_files`, rather than presenting `status=partial` with every cause list empty
- **AND** a removal whose model directory disappeared entirely SHALL omit all skip-cause keys, while declaration-invalid refusal rows carry entry evidence instead and remain outside this scenario

#### Scenario: Historical receipts without the additive unreadable key remain valid

- **WHEN** runtime validation, JSON Schema validation, or primary-receipt reconstruction reads a historical receipt carrying only status, missing, and invalid skip-cause evidence
- **THEN** the receipt validates and reconstructs exactly as before, because `unreadable_required_files` is optional

#### Scenario: Unreadable cause lists use the existing evidence bounds

- **WHEN** an unreadable-required-file list exceeds the receipt's collection or string bounds
- **THEN** the publisher/refresh evidence applies the same item and string limits as the other skip-cause lists, and runtime validation and JSON Schema validation agree on the resulting receipt

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

The guard's metadata comparison (`before != after`) SHALL be covered in
isolation as well: a covering test SHALL construct a divergence that the
content digest cannot see — content bytes and digest unchanged, one physical
identity field changed — and SHALL fail if the metadata comparison is
removed while the two content-divergence tests above stay green. `mode` is
the divergence field the covering test uses, because it needs neither
privileges nor timestamp granularity.

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

#### Scenario: Metadata changed during the read with content bytes untouched

- **WHEN** the destination holds `generation-a`, its bytes are never
  rewritten, and its `mode` is changed after the payload read but before the
  second preimage capture
- **THEN** the two captured preimages differ on `mode` while the content
  digest equals the captured `sha256`
- **AND** `read_provider_snapshot` raises `ProviderAtomicError` with reason
  `provider_preimage_changed` after exactly three reads through
  `read_bytes_limited_no_follow`
- **AND** the raise is produced by the metadata comparison alone, such that
  removing `before != after` makes this read succeed while the two
  content-divergence scenarios above still raise

### Requirement: The refresh timer's liveness is observable from independent signals before the freshness bound expires

The system SHALL provide a read-only, database-free health probe for the node-22
file-provider refresh lane that records the timer's unit-file state, active
state, next-elapse time, and the published manifest's age as four independent
signals, grades them into exactly one verdict, writes a bounded receipt, and
exits non-zero for every verdict other than healthy. The verdicts SHALL be
evaluated in a fixed precedence order — unreadable systemd evidence, then
expired manifest, then stopped timer, then not-enabled timer, then unscheduled
timer, then stale manifest, then unresolvable manifest — with the first matching
condition winning and the healthy verdict reachable only when no other condition
matches. An unresolvable manifest age SHALL NOT mask any timer verdict, and the
two manifest-age comparisons SHALL be skipped rather than defaulted when no age
could be resolved. The probe SHALL NOT
enable, disable, start, stop, restart, or reload any systemd unit, and SHALL NOT
write any file under the provider store.

#### Scenario: A stopped but still-enabled timer is detected

- **WHEN** the refresh timer reports an `ActiveState` other than `active`, the
  elapsed time since it became inactive exceeds the configured stopped-dwell,
  and the published manifest has not yet reached the consumer's 168-hour bound
- **THEN** the probe returns the `timer_stopped` verdict and a non-zero exit status
- **AND** the receipt records the unit-file state and active state as separate
  fields, so `enabled` alone can never be read as proof of liveness
- **AND** no systemd unit state is modified by the probe

#### Scenario: A deliberately stopped timer inside the operator dwell does not alarm

- **WHEN** the refresh timer reports `UnitFileState=enabled` with an
  `ActiveState` other than `active`, but the elapsed time since it became
  inactive is within the configured stopped-dwell threshold — the geometry of a
  live manual-publisher window, where the documented procedure stops the timer,
  runs the publisher, and starts it again
- **THEN** the probe does not return the `timer_stopped` verdict
- **AND** the remaining signals are still graded, so a stale or expired manifest
  in the same tick still produces a non-healthy verdict and a non-zero exit

#### Scenario: A timer left un-enabled is detected rather than graded healthy

- **WHEN** the refresh timer reports a `UnitFileState` other than `enabled` —
  including the `disabled` state the refresh installer deliberately leaves
  behind after an install or a rollback — and no higher-precedence condition
  matches
- **THEN** the probe returns the `timer_not_enabled` verdict and a non-zero exit status
- **AND** the healthy verdict is not reachable for such a timer, whether or not
  it happens to be active at that moment

#### Scenario: An enabled and active timer with no scheduled tick is detected

- **WHEN** the refresh timer is `active` but reports no next-elapse time, or a
  next-elapse time further away than the configured next-dwell threshold, and no
  higher-precedence condition matches
- **THEN** the probe returns the `timer_not_scheduled` verdict and a non-zero exit status

#### Scenario: Manifest age is graded independently of systemd state

- **WHEN** the most recent published provider manifest's generation time is older
  at or beyond the configured manifest-age threshold but not yet at the
  consumer's 168-hour bound, and no higher-precedence condition matches
- **THEN** the probe returns the `manifest_stale` verdict and a non-zero exit status
- **AND** when that age reaches or exceeds 168 hours the probe returns the
  distinct `manifest_expired` verdict
- **AND** every configurable threshold is range-checked so that it always leaves
  at least one full refresh cadence of margin below the consumer's 168-hour
  bound, and the stopped-dwell is additionally capped at one cadence; a
  configuration outside those ranges is rejected before any evidence is
  collected, and no accepted combination of thresholds grades a lane healthy
  once its timer has been enabled-but-inactive for longer than one cadence.
  This is deliberately scoped to the stopped-timer geometry: a timer that is
  active and due to tick shortly, whose manifest is merely older than one
  cadence, is healthy by design — the manifest-age threshold exists precisely to
  tolerate a slow-but-alive lane, and capping it at one cadence would defeat it

#### Scenario: The probe fails closed

- **WHEN** the systemd query cannot be executed or returns an error, or the
  timer is not active while reporting no parseable time at which it became
  inactive — leaving the stopped-dwell comparison undefined
- **THEN** the probe returns a non-healthy verdict and a non-zero exit status
- **AND** the healthy verdict is never produced as a fallback for missing evidence

#### Scenario: A failed refresh receipt does not mask a dead timer

- **WHEN** the latest refresh receipt cannot yield a published manifest's
  generation time — it is missing, unreadable, schema-invalid, or carries no
  registry provider, which is the shape a refresh run that fails before it
  assembles its provider list leaves behind — and the refresh timer is in a
  state that would otherwise grade as stopped or not enabled
- **THEN** the probe returns the timer verdict, not an evidence verdict, so the
  actionable fact is the one reported
- **AND** the manifest-age comparisons are skipped rather than treated as
  satisfied or violated

#### Scenario: The manifest age falls back to receipt history

- **WHEN** the latest refresh receipt cannot yield a generation time, but an
  earlier receipt in the runner's bounded history directory can
- **THEN** the probe resolves the manifest age from the most recent such
  receipt, ordered by the fixed-width UTC timestamp their filenames carry
- **AND** a candidate receipt reports the registry generation time the consumer
  would read: the post-publish time only when its outcome guarantees those bytes
  are on disk (published, published with a failed primary receipt, or dry-run),
  and otherwise the pre-run time, so a run that rolled the registry back can never
  make the manifest look fresher than it is
- **AND** the receipt records which source answered, distinguishing the latest
  receipt, a named history receipt, and no available source
- **AND** the scan is bounded in both the number of directory entries considered
  and the number of receipts opened, and each read refuses symlinks and is size-limited

#### Scenario: No resolvable manifest age is its own verdict

- **WHEN** neither the latest refresh receipt nor any receipt in the bounded
  history scan yields a registry generation time it can trust, and no
  higher-precedence condition matches
- **THEN** the probe returns a distinct unresolvable-manifest verdict and a
  non-zero exit status
- **AND** the healthy verdict is not reachable, and the recorded source is the
  no-source value

#### Scenario: The compute scheduler units are untouched

- **WHEN** the probe or its installer runs in any mode
- **THEN** the compute scheduler's timer is identical before and after the run in
  both its unit-file state and its active state
- **AND** the compute scheduler's service — a oneshot activated by that timer on
  its own cadence, and therefore not a unit whose active state any installer can
  hold still — is identical before and after the run in its unit-file state
- **AND** the comparison is performed per unit type for this reason, so that a
  oneshot activating on schedule mid-run is never mistaken for an installer
  having touched it
- **AND** the installer's protected-state baseline is captured at the start of
  every invocation, so the comparison always describes that one invocation and
  no on-disk baseline is ever interpreted across versions of the installer

#### Scenario: The probe installer reports a disarmed probe only when it really is

- **WHEN** the probe installer's install has placed the unit files and reloaded
  systemd and the probe timer or service then reads back as enabled or active,
  or its rollback runs and systemd refuses to disable or stop the probe timer or
  service
- **THEN** the run exits non-zero and does not report an installed-stopped or
  rolled-back status
- **AND** success is reported only after re-reading each probe unit on its own
  shows neither is enabled nor active, where a failed probe service left behind
  by a non-healthy verdict counts as disarmed and an unreachable user manager
  does not
- **AND** disarmed means inert rather than removed: rollback restores the unit
  files that preceded the first install recorded in the installer's restore
  baseline and never re-arms them

#### Scenario: The probe installer never disarms an armed probe or rewrites its baseline

- **WHEN** the probe installer's install runs while the probe timer or service
  is enabled or active
- **THEN** it exits non-zero, tells the operator to roll back first, and leaves
  every unit, unit file, and file under the installer's state root unchanged,
  including the per-invocation protected-state capture
- **AND** when it runs against a disarmed probe whose restore baseline is already
  marked complete, it keeps that baseline, so installing twice and then rolling
  back restores the unit files that preceded the first install
- **AND** a baseline whose completion marker is absent is captured afresh, so an
  interrupted first install never leaves a partial baseline in force

### Requirement: The tracked DB-free scheduler env template carries every key the documentation declares mandatory

The tracked template for the node-22 database-free scheduler environment SHALL
contain every environment key that the environment documentation declares
mandatory for that lane, including the orchestrator terminal stage that stops
the chain before the database-dependent publish stage.

#### Scenario: A node rebuilt from the template stops at the documented terminal stage

- **WHEN** the node-22 database-free scheduler environment is rebuilt from its
  single tracked template
- **THEN** the template sets the orchestrator terminal stage to
  `forecast_state_save_qc` and requires forecast warm start
- **AND** every key the environment documentation names as mandatory for this
  lane is present in the template, verified by an automated check rather than by
  manual reading

### Requirement: A dry-run refresh produces self-valid evidence without mutating any provider

The refresh runner SHALL, in dry-run mode under direct-grid authority with a
worker registry mirror configured, report the canonical registry and the worker
registry mirror with the same prospective model count, produce a terminal
receipt that passes its own validation, persist that receipt to both history and
latest, release its emergency reservation, and leave every provider byte
unchanged. Dry-run mode SHALL NOT commit any provider bytes and SHALL NOT
execute the worker-mirror publisher, and a failure occurring after the precommit
gate SHALL retain its own failure reason.

#### Scenario: Dry-run with a configured worker mirror succeeds

- **WHEN** a dry-run refresh runs with direct-grid authority required, a worker
  registry mirror configured, and canonical and mirror preimages identical
- **THEN** the run exits successfully with `outcome=dry_run`,
  `reason=dry_run_complete`, and `phase=complete`
- **AND** the receipt reports the registry and `registry_worker_mirror`
  `entry_count` values as equal to the prospective model count derived from this
  run, for a single-model and a multi-model set alike
- **AND** the registry, worker mirror, readiness, and state files are byte- and
  preimage-identical before and after the run
- **AND** the receipt is present in both the history directory and the latest
  pointer, and this run's emergency reservation is removed without leaving an
  empty reserved slot
- **AND** a dry-run over an empty model set continues to fail closed with
  `provider_invalid` on both the direct-grid and the non-direct-grid path,
  terminating as a failed outcome carrying no provider evidence, so no
  successful zero-count dry-run receipt is ever produced

#### Scenario: A dry-run failure after the gate keeps its own reason

- **WHEN** a dry-run refresh fails after the precommit gate for a genuine
  provider or gate reason
- **THEN** the reported reason is that genuine reason
- **AND** it is not replaced by a receipt-assembly failure such as
  `primary_receipt_failed`

### Requirement: The refresh installer backs out every failure once, reads the result back, and never rewrites its restore baseline

The node-22 file-provider refresh installer SHALL run under `errtrace`. Every
failure after its first mutation SHALL run exactly one restore handler, and
only in the installer's main shell. That handler SHALL attempt every restore
step whatever the earlier steps did, SHALL always finish with read-back
assertions, and SHALL exit non-zero without printing a status line.

Every restore SHALL be confirmed by reading the refresh units back against
their restore target before the installer reports success:

- the timer is compared on unit-file state and active state;
- the service, a timer-driven oneshot, is compared on unit-file state.

The restore target of a rollback and of a failed install SHALL be the recorded
restore baseline. The restore target of a failed enable SHALL be the state that
invocation started from.

The compute-scheduler comparison SHALL be per unit type, captured at the start
of each invocation. It SHALL NOT read any baseline written by another
invocation.

The install action SHALL refuse without mutation while the refresh timer or
service is armed. It SHALL NOT overwrite a restore baseline that is already
recorded.

A recorded unit state that does not have exactly the expected fields and lines
SHALL fail rather than be interpreted.

#### Scenario: A failure before any mutation changes nothing

- **WHEN** an action fails before its first mutation: the current receipt does
  not validate for enable, the install refuses, the install or the rollback
  finds an existing baseline malformed, or the rollback finds its baseline
  missing
- **THEN** the run exits non-zero without a status line, issues no mutating
  systemd call, runs no restore, and changes no unit file or baseline

#### Scenario: A failure inside a function backs the action out

- **WHEN** an assertion or command inside a function fails after the
  installer's first mutation, in install or in enable
- **THEN** the restore handler runs exactly once, in the main shell, including
  when the failing command ran inside a command substitution
- **AND** the run exits non-zero and prints no status line

#### Scenario: A failing restore step does not cut the restore short

- **WHEN** any single restore step fails inside the handler, whether a unit-file
  restore, a daemon reload, or an enable, disable, start, or stop
- **THEN** every later restore step is still attempted
- **AND** both read-back assertions still run
- **AND** the run exits non-zero without a status line

#### Scenario: Rollback success is decided by reading the units back

- **WHEN** a rollback's restore calls all return success but the refresh timer
  reads back in a state other than the recorded baseline
- **THEN** the rollback exits non-zero and does not report rolled back
- **AND** it reports rolled back only when both hold on read-back: the refresh
  units equal the baseline, and the compute-scheduler units are unchanged

#### Scenario: A compute-scheduler oneshot firing mid-run is not a divergence

- **WHEN** the compute scheduler's service activates on its own timer while
  an installer action is running
- **THEN** the action does not abort, because that service is compared on its
  unit-file state only
- **AND** the compute scheduler's timer is still compared on both its unit-file
  state and its active state

#### Scenario: A legacy scheduler baseline file is ignored

- **WHEN** the installer's state root holds a scheduler baseline file written
  by an earlier installer version, in any format
- **THEN** no action reads or rewrites it
- **AND** enable and rollback succeed or fail on the protected state captured
  by their own invocation

#### Scenario: Installing never disarms an armed lane

- **WHEN** the install action runs while the refresh timer or service reads
  back as enabled or active
- **THEN** it exits non-zero, tells the operator to roll back first, and issues
  no mutating systemd call
- **AND** it changes no unit file and no recorded baseline

#### Scenario: Install reports installed-stopped only when the lane reads back disarmed

- **WHEN** the install action has placed the unit files and reloaded systemd,
  and the refresh timer or service then reads back as enabled or active
- **THEN** the install exits non-zero, runs its restore handler, and does not
  report installed-stopped
- **AND** it reports installed-stopped only after the refresh timer and service
  each read back as neither enabled nor active, using the same disarmed
  definition as the refusal above

#### Scenario: A second install keeps the first install's restore baseline

- **WHEN** the install action runs on a disarmed lane whose restore baseline is
  already recorded
- **THEN** the baseline and its saved unit files are left byte-identical
- **AND** a later rollback restores the state and unit files that preceded the
  first install
- **AND** the baseline counts as recorded only once it has been written
  completely, so an interrupted first install is recaptured by the next one

#### Scenario: A malformed recorded state fails

- **WHEN** a recorded unit state has other than exactly two non-empty fields,
  or the recorded baseline has other than exactly one line per refresh unit
- **THEN** a rollback fails before its first mutation
- **AND** inside a restore handler the failure counts as a failed step, while
  the read-back still runs

### Requirement: A replace-uncertain refresh receipt describes the bytes on disk for every provider whose rollback was verified

When a refresh transaction's provider rollback is verified but the run must
still report `replace_uncertain`, because another lane's outcome is unknown, the
receipt SHALL describe the restored generation for each committed provider whose
rollback was verified. Its post-run digest, schema version, generation time and
payload checksum SHALL describe the bytes on disk when the receipt is written.
The receipt SHALL NOT describe the generation that was published and then rolled
back.

This SHALL NOT change the receipt schema. It SHALL NOT change any receipt whose
rollback was not verified, and it SHALL NOT change the outcome-based field
selection that the refresh-timer health probe applies to every receipt.

#### Scenario: A later lane's uncertainty does not leave restored providers claiming rolled-back bytes

- **WHEN** the registry, its worker mirror and the readiness index are
  published, a later lane's write leaves ownership unknowable, and the rollback
  of the three published providers is verified
- **THEN** the receipt's outcome is `replace_uncertain`
- **AND** for each of the three providers its post-run digest equals the digest
  of the bytes on disk, or is absent when the path is absent
- **AND** its post-run generation time is never newer than the manifest on disk
- **AND** the receipt, and any emergency record carrying it, still validates
  against the unchanged schema

#### Scenario: An unverified rollback keeps its evidence unchanged

- **WHEN** the provider rollback itself cannot be verified
- **THEN** the `replace_uncertain` receipt carries the committed evidence
  exactly as before this change
- **AND** readers keep choosing the registry generation time by outcome, because
  such a receipt may still describe bytes that are not on disk
