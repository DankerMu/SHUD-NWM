# fingerprint-gated-state-clone — delta

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

That admission predicate binds the ANSWER, and the layer that discharges each
clause differs by plane. On the file state-snapshot index plane one filter
discharges all three clauses while SELECTING the entries. On the database plane
the row-selection statement discharges only presence and a byte-literal
difference from the row's own `model_id`; the non-empty clause, and the
difference clause under whitespace normalisation, are discharged downstream of
selection. Because selection there takes the earliest row and stops, a
provenance-corrupt row — a blank `cloned_from_model_id`, or one naming the row's
own `model_id` with surrounding whitespace — can be selected, be correctly
refused lineage downstream, and thereby MASK a later legitimate clone row that
the file plane would have found. Both planes give the same answer for that row;
they can still resolve different `t*` for the same model. This masking is a
known open gap, not a claim of compliance: on the database plane, row selection
SHALL eventually normalise `cloned_from_model_id` before applying both clauses,
so a corrupt row is skipped rather than merely refused. Closing it requires
BOTH an emptiness test and a normalised self-reference test — an emptiness test
alone leaves the padded self-reference standing.

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

## ADDED Requirements

### Requirement: The clone refuses a self-clone before every other gate

`fingerprint_gated_state_clone` SHALL refuse, fail-closed, when the source
model identity and the target model identity are the same value. The refusal
SHALL be evaluated BEFORE every other gate — before the no-reverse-clone
classification of the target's forcing-mapping manifest, before the degenerate
gate-input checks, before the qualified-source lookup, and before any
fingerprint computation — so a self-clone is refused on identity alone
regardless of how valid the remaining inputs are.

The refusal SHALL use the existing refusal channel: a compact audit record
naming a distinguished `refusal_scope` for self-clone, and the fail-closed
result. No `hydro.state_snapshot` row SHALL be written and no physical state
file SHALL be touched.

The reason the check belongs inside the gate rather than in each caller: the
clone row's `state_id` is minted deterministically from the TARGET `model_id`
plus the preserved source/valid-time/cycle/lead inputs, so with equal source and
target identities the minted `state_id` is byte-identical to the source row's
own `state_id` and the upsert overwrites the real source row in place — turning
it into a row that names itself as its own predecessor and losing the original
provenance irrecoverably. A reader-side guard can reject such a row afterwards
but cannot restore the overwritten one.

#### Scenario: A self-clone is refused in fix-forward mode

- **WHEN** `fingerprint_gated_state_clone` is invoked with the same value for
  the source and target model identities in the ten-surface `fix_forward` mode
- **THEN** the call returns refused with the self-clone `refusal_scope`
- **THEN** a refusal audit record is emitted naming that scope
- **THEN** no snapshot upsert is performed.

#### Scenario: A self-clone is refused in recalibration mode

- **WHEN** the same invocation is made in the eight-surface `recalibration`
  mode, including the branch where the target's recorded
  `hydrologic_core_fingerprint` is absent and the evidence cross-check is
  skipped
- **THEN** the call returns refused with the self-clone `refusal_scope` and no
  snapshot upsert is performed.

#### Scenario: Self-clone refusal outranks every other refusal

- **WHEN** a self-clone invocation supplies a valid direct-grid forcing-mapping
  manifest and non-empty state-schema and solver-config gate inputs, so no
  other gate would refuse
- **THEN** the refusal scope is the self-clone scope, not the
  target-not-direct-grid scope and not the degenerate-gate-inputs scope.
