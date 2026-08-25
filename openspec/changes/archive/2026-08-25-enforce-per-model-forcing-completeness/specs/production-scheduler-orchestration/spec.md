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
segments, after normalising only for the shapes the producer and the handoff lane
are already known to record:

1. a trailing `/` is removed (the producer records the package as a directory
   uri; the handoff lane stores the same reference with the slash stripped, and
   both shapes coexist);
2. a final segment equal to the package manifest filename is removed (a recorded
   reference may already be the manifest FILE key, one segment deeper than the
   package prefix).

After those two removals, the last two segments MUST equal this candidate's
`basin_version_id` and `model_id` in that order.

The recorded reference MUST NOT be prefix-normalised before the comparison,
because a foreign object-store prefix makes normalisation raise and would be
reported as a false absence. Only the two trailing-shape removals above are
permitted.

A reference with fewer than two segments after those removals MUST be treated as
not bound, exactly like a foreign one.

#### Scenario: A superseded model's package does not witness the successor

- **GIVEN** a candidate whose inherited state records a `forcing_package_uri`
  under a different `model_id`
- **AND** that package physically exists in the object store
- **WHEN** the per-model forcing witness is consulted
- **THEN** the recorded reference is rejected as foreign
- **AND** the identity-derived tier is consulted in its place
- **AND** the provenance annotation names the rejection

#### Scenario: A package under a different basin does not witness this candidate

- **GIVEN** a candidate whose inherited state records a reference whose last two
  segments are a DIFFERENT `basin_version_id` followed by this candidate's own
  `model_id`
- **WHEN** the per-model forcing witness is consulted
- **THEN** the recorded reference is rejected as foreign
- **AND** the identity-derived tier is consulted in its place

Both halves of the identity pair are load-bearing: a reference is bound only when
the `basin_version_id` segment AND the `model_id` segment both match. Checking
either alone re-opens the fail-open this requirement exists to close.

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

#### Scenario: The candidate's own reference is admitted in every shape the producer records

- **GIVEN** a candidate whose recorded reference names its own
  `basin_version_id` and `model_id`
- **WHEN** that reference is the package directory uri with a trailing `/`, or
  the same reference with the slash stripped, or the package manifest file key
  one segment deeper
- **THEN** each shape is admitted as this candidate's witness
- **AND** none of them falls through to the identity-derived tier

#### Scenario: A reference too short to carry the identity pair is not bound

- **GIVEN** a recorded reference that has fewer than two key segments left after
  the trailing-shape removals
- **WHEN** the per-model forcing witness is consulted
- **THEN** it is treated as not bound, exactly like a foreign reference
- **AND** the identity-derived tier is consulted in its place
