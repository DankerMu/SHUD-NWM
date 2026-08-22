# Spec Delta: fingerprint-gated-state-clone

## ADDED Requirements

### Requirement: cloned_from provenance SHALL have a scheduling-time consumer

A clone row's `cloned_from_model_id` and `clone_gate_kind` SHALL be readable
at scheduling time on both persistence planes, and SHALL be consumed by the
scheduler's cycle-completion scope and cohort-admission decisions (see
`cross-cycle-warm-start-chaining`). They SHALL NOT remain write-only
provenance.

On the file state-snapshot index plane the fields SHALL survive entry
normalisation into the loaded index snapshot, so a scheduling pass resolves
lineage from data it has already loaded and performs no additional read. On the
database plane the resolution source SHALL be an **earliest**-clone-row read
under the model's own `model_id`, ordered `(valid_time, created_at)` ascending
— the model's existence-start. The publisher's descending reader
(`get_latest_clone_row_for_model_source`) SHALL NOT be the resolution source:
its ordering serves mirroring the just-committed row, not answering an
existence question, and reusing it would let a backdated re-activation
retroactively exclude cycles the identity actually ran.

A reader SHALL tolerate the absence of these fields on an older or non-clone
entry, treating absence as "no lineage" rather than as an error.

#### Scenario: A scheduling pass resolves lineage without an extra read

- **WHEN** a db-free scheduling pass has loaded the file state-snapshot index
  and needs the lineage of a model that carries a clone row
- **THEN** it resolves `cloned_from_model_id` and the clone row's `valid_time`
  from the already-loaded index entries
- **THEN** it issues no additional read against the index or the object store.

#### Scenario: Absent provenance means no lineage, not an error

- **WHEN** a state-index entry or snapshot row carries no
  `cloned_from_model_id`
- **THEN** lineage resolution yields "no lineage" for that model and source
- **THEN** the model is scored and admitted exactly as a model that never
  cloned.
