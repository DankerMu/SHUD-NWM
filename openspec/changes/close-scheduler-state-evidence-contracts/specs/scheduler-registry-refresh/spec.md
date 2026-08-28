## MODIFIED Requirements

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
