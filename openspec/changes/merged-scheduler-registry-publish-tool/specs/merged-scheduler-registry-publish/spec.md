## ADDED Requirements

### Requirement: Merged registry publish is planned before it is applied

The merged scheduler registry publish tool SHALL write neither manifest nor any backup unless `--apply` is
given, and with `--apply` SHALL refuse unless the dry-run receipt of the same succession recorded the same
operations, canonical content and merged model list.

#### Scenario: Dry-run of a replace

- **WHEN** the tool runs without `--apply` with a valid replace operation
- **THEN** the canonical manifest, the mirror manifest and their directory listing are unchanged except for the tool's own receipt
- **AND** the report lists the introduced and removed model ids and the predicted manifest size

#### Scenario: Apply after the canonical manifest changed

- **WHEN** `--apply` runs and the canonical manifest's `models` differ from what the dry-run receipt recorded
- **THEN** the tool exits non-zero before creating any backup and both manifests are unchanged

### Requirement: New rows come from a provisioned registry

The tool SHALL take every introduced row verbatim from the registry written by the provision apply of the same
succession and SHALL refuse when that receipt is missing, when the registry file's sha256 differs from the
receipt, or when the introduced model ids differ from the receipt's models.

#### Scenario: Introduced id not provisioned in this succession

- **WHEN** an operation introduces a `model_id` that the provision apply receipt does not list
- **THEN** the tool exits non-zero and writes neither manifest

### Requirement: Canonical and mirror never end different after a completed run

The tool SHALL publish the canonical manifest with a compare-and-swap on the content it read, then the mirror
with the same rows and `generated_at`, and SHALL restore every manifest it committed when the run does not end with both published and equal.

#### Scenario: Mirror publish fails

- **WHEN** the canonical manifest was published and the mirror publish fails
- **THEN** the canonical manifest is restored to its previous bytes, the exit status is non-zero and the apply receipt records `rolled_back`

#### Scenario: Successful apply

- **WHEN** an apply completes
- **THEN** both manifests have the same sha256, each has a backup of its previous bytes, and the apply receipt records `published`
