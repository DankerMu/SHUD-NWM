# forcing-production Specification Delta

## ADDED Requirements

### Requirement: Per-model forcing is restored by replaying production, never by copying

Forcing for an already-produced cycle MUST be restored by re-running the producer under
the new `model_id` whenever a model's identity changed without its station bindings
moving. The superseded package MUST NOT be copied or byte-rewritten into the new model's
path.

The restoration MUST prove equivalence against the superseded package: the SHUD station
inputs MUST match byte for byte, and the data members that carry identity strings MUST
match once those strings are normalised away. Package manifests carrying member checksums
are excluded from that comparison, because their bytes must differ when the members' do.

#### Scenario: An identity-only change is replayed and verified

- **WHEN** a model's `model_id` changed and its `station_bindings` are identical after the
  identity prefix is removed
- **AND** a cycle holds forcing under the old `model_id` but not the new one
- **THEN** the producer is run for that `(source, cycle, model_id)`
- **AND** every `shud/` station file in the new package is byte-identical to the old one
- **AND** the tsd, debug, and payload members are identical after identity normalisation
- **AND** the receipt records the work item as verified

#### Scenario: Moved stations are refused rather than backfilled

- **WHEN** a model's `model_id` changed and its normalised `station_bindings` still differ
  from the superseded model's
- **THEN** no forcing is produced for that model
- **AND** the receipt records it as a skipped re-binding
- **AND** the command exits non-zero

#### Scenario: A topology diff is refused before any production

- **WHEN** the two registry manifests do not describe the same `(sp_att_path, source_id)` set
- **THEN** the command refuses with a diagnosable error naming the divergent keys
- **AND** no producer invocation is made

#### Scenario: An upper-cased source is found under its lower-cased path

- **WHEN** a renamed model's canonical source id is upper-cased
- **THEN** its existing forcing is discovered under the lower-cased path segment
- **AND** the work item is planned rather than silently omitted

### Requirement: A run that failed on a missing artifact is restarted only through the manual-retry marker

Restarting a forecast run that failed on an absent forcing package MUST go through the
policy-gated manual-retry marker, scoped to one named run. Such a run is classified as a
permanent failure and does not resume when the package is restored; journal rows MUST NOT
be edited to achieve the same effect.

#### Scenario: The marker targets the per-run row, not the cohort master

- **WHEN** an operator previews a manual retry for a failed run whose cycle also has a
  forecast cohort-master job
- **THEN** the preview names the per-run job row
- **AND** no marker is written without an explicit execute flag

#### Scenario: A marked run is selected again by the next pass

- **WHEN** a manual-retry marker has been recorded for a run that failed with a missing
  forcing artifact
- **AND** the forcing package now exists under that run's `model_id`
- **THEN** the next scheduler pass no longer reports the candidate as permanently blocked
- **AND** runs that were not marked remain blocked

#### Scenario: Refusals are reported, not raised

- **WHEN** the named run has an active job, or has no eligible failed job
- **THEN** the command reports the refusal reason in its receipt
- **AND** exits non-zero without writing a marker
