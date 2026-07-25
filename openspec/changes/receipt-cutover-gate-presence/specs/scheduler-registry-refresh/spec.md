# scheduler-registry-refresh — delta (receipt-cutover-gate-presence)

## MODIFIED Requirements

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

### Requirement: The receipt validator SHALL bind dry_run receipts to the reconciliation constraints that hold in id-only mode

`_enforce_registry_classification_reconciliation` SHALL apply the id-only constraint set to every receipt whose classification carries `mode="id_only"` (falling back to `outcome="dry_run"` keying when the legacy receipt has no mode field), rejecting any such receipt whose classification violates a constraint that the id-only classify path guarantees by construction, specifically: `removed.total` must be zero, `package_changed.total` and `declared_cutovers.total` must be zero, every `refused` entry's reason must be `registry_cutover_declaration_invalid` (the synthetic `__declaration__` marker is the only refusal the writer can attach to an id-only classification; the legacy outcome-keyed fallback keeps rejecting all refused entries as before), a receipt-level cutover refusal `reason` still requires `refused.total >= 1` (writer sets the refusal reason and appends the refused row in the same action — the two must stand or fall together), `previous_registry_sha256` and `previous_model_count` must be null together or non-null together (with a non-boolean integer count >= 0), `new_registry_sha256` must be null (an id-only classification only arises from dry_run, which never publishes a registry), when no previous registry is recorded `unchanged.total` must also be zero (with an empty `previous_by_id` every prospective row classifies as added — the dry_run dual of the bootstrap sum invariant), and when a previous registry exists `unchanged.total` must not exceed `previous_model_count`. The validator SHALL NOT apply the full previous-side equality (`unchanged + package_changed + removed == previous_model_count`) to id-only classifications, because the id-only path never evaluates removals — regardless of the receipt's terminal outcome. On the mode-keyed id-only arm the validator SHALL additionally require the `refused` group to be untruncated with `total == len(items)` and `total <= 1` and every entry's `model_id` equal to the synthetic `__declaration__` marker, and SHALL reject any `outcome="dry_run"` receipt whose refused total is non-zero (a declaration failure always terminates with `outcome="failed"`, so no legal writer emits a dry_run refusal); the legacy no-mode arm keeps rejecting all refused entries unchanged.

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
