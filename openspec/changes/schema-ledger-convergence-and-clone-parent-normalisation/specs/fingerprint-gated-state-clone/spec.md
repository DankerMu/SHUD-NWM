## ADDED Requirements

### Requirement: Both persistence planes SHALL normalise cloned_from_model_id identically before judging it

"Present, non-empty, and different from the row's own `model_id`" SHALL be judged on `cloned_from_model_id` with surrounding whitespace removed, where whitespace is exactly the set of characters Python's `str.isspace()` accepts. The comparison with `model_id` SHALL use the raw `model_id`. On the DB plane this SHALL be enforced by the SQL predicate of the earliest-clone-row reader, so that `LIMIT 1` never selects a row the file plane would skip. The resolver's own normalisation SHALL stay as defence in depth. The publisher's latest-clone-row reader is not governed by this requirement.

#### Scenario: A whitespace-only parent does not mask a later clone row

- **WHEN** the earliest clone row of a `(model_id, source_id)` has a `cloned_from_model_id` made only of whitespace (including tab, newline or U+3000) and a later clone row names a real predecessor
- **THEN** both planes SHALL resolve the lineage cutover from the later row

#### Scenario: A padded self-reference does not mask a later clone row

- **WHEN** the earliest clone row's `cloned_from_model_id` is the row's own `model_id` with surrounding whitespace and a later clone row names a real predecessor
- **THEN** both planes SHALL resolve the lineage cutover from the later row

#### Scenario: The planes agree

- **WHEN** the same set of clone rows is written to the file plane and to the DB plane
- **THEN** the two planes SHALL resolve the same `LineageCutover`

## MODIFIED Requirements

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

Lineage admission SHALL be keyed on `cloned_from_model_id` alone — present,
non-empty, and different from the row's own `model_id`. `clone_gate_fingerprint`
is provenance recording WHICH gate admitted the clone and at WHAT value; it
SHALL NOT be an admission condition for lineage on either persistence plane. A
clone row carrying `cloned_from_model_id` but no `clone_gate_fingerprint` SHALL
therefore confer lineage, identically on both planes: requiring the fingerprint
would reject such a row and move `t*` LATER, which silently removes the model
from cycles it genuinely has a gap in, whereas admitting it leaves at most a
loud stuck gap.

That admission predicate binds the ANSWER, and both planes discharge every
clause while SELECTING the row. On the file state-snapshot index plane one
filter discharges all three clauses on the stripped value. On the database
plane the row-selection statement discharges presence, non-emptiness and the
difference from the row's own `model_id` on `cloned_from_model_id` with
surrounding whitespace removed (#2392; see the requirement "Both persistence
planes SHALL normalise cloned_from_model_id identically before judging it").
A provenance-corrupt row — a blank `cloned_from_model_id`, or one naming the
row's own `model_id` with surrounding whitespace — is therefore skipped by
selection on both planes and cannot MASK a later legitimate clone row; both
planes resolve the same `t*` for the same model. Downstream refusal of such a
row stays as defence in depth.

Lineage resolution SHALL distinguish a resolution FAILURE from a resolved "no
lineage". A state-snapshot index that exists but cannot be read, parsed, or
validated, a persistence-plane read that raises, and — on a plane whose
provider returns a structured signal the resolver can shape-check — a provider
that violates the resolution contract are failures; an absent provider, an
index that has
never been published, absent provenance, and provenance that does not establish
a predecessor are resolved answers meaning "no lineage". A never-published index
SHALL NOT be treated as a failure: a deployment that has performed no clone is a
healthy deployment with no lineage, and failing it would emit an operator signal
on every resolution forever. A failure SHALL surface
an operator-visible signal naming the model, the source, and the reason, and
SHALL NOT be memoized as "no lineage": a later resolution attempt for the same
`(model_id, source_id)` SHALL re-attempt the read rather than replay the
failure. A resolved "no lineage" MAY be memoized.

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

#### Scenario: A missing clone_gate_fingerprint does not withhold lineage

- **WHEN** a clone row or index entry carries `cloned_from_model_id` naming a
  different model but carries no `clone_gate_fingerprint`
- **THEN** both persistence planes admit it as the model's existence-start and
  resolve the same `t*` from its `valid_time`
- **THEN** neither plane treats the absent fingerprint as a reason to withhold
  lineage.

#### Scenario: A never-published index resolves to no lineage without a failure signal

- **WHEN** a lineage resolution runs on a deployment whose state-snapshot index
  has never been published
- **THEN** resolution yields "no lineage" for every model and source
- **THEN** no failure signal is emitted and the answer may be memoized, so a
  healthy deployment that has performed no clone stays quiet.

#### Scenario: A resolution failure is not remembered as "no lineage"

- **WHEN** a lineage resolution for `(model_id, source_id)` fails — the
  persistence-plane read raises, or a published state-snapshot index cannot be
  read, parsed, or validated
- **THEN** an operator-visible signal names the model, the source, and the
  failure reason
- **THEN** the failure is not stored as that pair's resolved lineage, and the
  next resolution for the same pair re-attempts the read
- **THEN** once the underlying condition clears, the pair resolves to its true
  cutover without restarting the scheduler process.
