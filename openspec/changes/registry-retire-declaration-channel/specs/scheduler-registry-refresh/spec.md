# scheduler-registry-refresh (delta)

## ADDED Requirements

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

## MODIFIED Requirements

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
