# scheduler-registry-refresh — delta (classification-mode-keyed-reconciliation)

## ADDED Requirements

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

#### Scenario: Forged mode/outcome combinations are rejected

- **WHEN** a receipt carries `outcome="dry_run"` with
  `classification.mode="full"`, or `outcome="published"` with
  `classification.mode="id_only"`, or a `mode` outside `id_only`/`full`
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

## MODIFIED Requirements

### Requirement: The receipt validator SHALL bind dry_run receipts to the reconciliation constraints that hold in id-only mode

`_enforce_registry_classification_reconciliation` SHALL apply the id-only constraint set to every receipt whose classification carries `mode="id_only"` (falling back to `outcome="dry_run"` keying when the legacy receipt has no mode field), rejecting any such receipt whose classification violates a constraint that the id-only classify path guarantees by construction, specifically: `removed.total` must be zero, `package_changed.total` and `declared_cutovers.total` must be zero, every `refused` entry's reason must be `registry_cutover_declaration_invalid` (the synthetic `__declaration__` marker is the only refusal the writer can attach to an id-only classification; the legacy outcome-keyed fallback keeps rejecting all refused entries as before), `previous_registry_sha256` and `previous_model_count` must be null together or non-null together (with a non-boolean integer count >= 0), `new_registry_sha256` must be null (an id-only classification only arises from dry_run, which never publishes a registry), when no previous registry is recorded `unchanged.total` must also be zero (with an empty `previous_by_id` every prospective row classifies as added — the dry_run dual of the bootstrap sum invariant), and when a previous registry exists `unchanged.total` must not exceed `previous_model_count`. The validator SHALL NOT apply the full previous-side equality (`unchanged + package_changed + removed == previous_model_count`) to id-only classifications, because the id-only path never evaluates removals — regardless of the receipt's terminal outcome.

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
