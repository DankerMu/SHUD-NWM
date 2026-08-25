# production-scheduler-orchestration Specification Delta

## ADDED Requirements

### Requirement: A recorded forcing reference SHALL witness only the candidate it belongs to

A `forcing_package_uri` recorded in a candidate's inherited state SHALL be
admitted as this candidate's witness only when its object key
names this candidate's own `<basin_version_id>/<model_id>` pair. A reference
naming any other model MUST be treated exactly as an absent reference: the
identity-derived tier runs instead, and the rejection is named in the provenance
annotation.

The comparison MUST be made on the recorded reference's own trailing key
segments. The recorded reference MUST NOT be prefix-normalised before the
comparison, because a foreign object-store prefix makes normalisation raise and
would be reported as a false absence.

#### Scenario: A superseded model's package does not witness the successor

- **GIVEN** a candidate whose inherited state records a `forcing_package_uri`
  under a different `model_id`
- **AND** that package physically exists in the object store
- **WHEN** the per-model forcing witness is consulted
- **THEN** the recorded reference is rejected as foreign
- **AND** the identity-derived tier is consulted in its place
- **AND** the provenance annotation names the rejection

#### Scenario: A reference naming the candidate's own model is probed as before

- **GIVEN** a candidate whose inherited state records a `forcing_package_uri`
  whose key names that candidate's own `basin_version_id` and `model_id`
- **WHEN** the per-model forcing witness is consulted
- **THEN** the reference is probed exactly as it was before this requirement
- **AND** the derived witness object remains the package manifest file key from
  the single shared derivation

#### Scenario: Existing containments are unchanged by the binding check

- **GIVEN** a recorded reference that is a withheld `[object-uri]` placeholder
- **WHEN** the per-model forcing witness is consulted
- **THEN** it keeps taking the recovery path it took before this requirement,
  and the binding check does not reclassify it
- **AND** a probe that cannot read its object still reports "cannot determine"
  rather than "package absent"
- **AND** no probe fault escapes the decision path
