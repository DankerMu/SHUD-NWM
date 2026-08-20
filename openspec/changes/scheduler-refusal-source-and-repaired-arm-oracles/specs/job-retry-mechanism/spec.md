# job-retry-mechanism Specification Delta

## ADDED Requirements

### Requirement: The single consulted permanence refusal source MUST have format-insensitive regression coverage

The single shared permanence judgement SHALL be pinned by regression coverage that survives reformatting and is not defeated by renaming.

The judgement is the one required by "Pre-Guard Evidence Channels Consult
Permanence". The coverage is two conjoined guards over
`services/orchestrator/scheduler_state_failure.py`:

- a **structural** guard asserting the module's complete
  `{module-level constant -> consuming function names}` mapping against a
  pinned expected mapping, where "module-level constant" covers both plain
  and annotated assignments (the module's two remedy tables are annotated
  assignments, and their values name other constants rather than containing
  a set literal, so neither an assignment-kind restriction nor a value-shape
  filter may narrow the subject). The subject SHALL be fail-closed **as an
  enumerated accept-set with a catch-all refusal**, not as a list of refused
  forms: any module-level statement form outside the accept-set SHALL make
  the guard refuse rather than pass over it silently. A refuse-list is not
  sufficient — an implementation refusing only the enumerated forms stays
  green, with a byte-identical mapping, while a second refusal list hides in
  a `match` body, a PEP 695 `type` alias, or an augmented assignment; each of
  those has been measured on Python 3.12+, and the `match` case has been
  measured to flip the shared judgement's verdict on a real input (the PEP 695
  alias is a parse error rather than a catch-all refusal on the 3.11
  interpreter this project's CI pins, which refuses it earlier rather than
  later). Because a syntactic subject cannot see a name bound at run time
  rather than in the module body — a `global` install being the measured
  example — the guard SHALL additionally cross-check the source inventory
  against the imported module object: every module-level name the module
  object carries SHALL have been inventoried from the source, every
  inventoried constant SHALL be present on the module object, and every
  inventoried name SHALL carry a runtime object of the kind its binding form
  declares, since a `def` name resolving to a data value is a run-time rebind
  and not a definition. The guard SHALL also refuse a star-import, whose bound
  names no source inventory can enumerate; and
- a **behavioural** guard asserting that, on the recorded-code domain
  (`code_recorded=True`), `_downstream_failure_restartable`'s verdict is
  determined by `limit_exhausted` and `permanent` alone and is constant
  across both a reason-code axis and a classifier axis. The code axis SHALL
  include three of the five codes the retired downstream blacklist carried
  (`INVALID_MANIFEST`, `MANIFEST_SCHEMA_INVALID`, `MALFORMED_INPUT`); its
  other two, `OUT_OF_MEMORY` and `POLICY_BLOCKED`, remain live permanence
  codes in `_REMEDY_NON_CAUSAL_CODES` and lie off the axis. The classifier
  axis SHALL include every member of `_DOWNSTREAM_PLACEHOLDER_REFUSAL_CLASSIFIERS`,
  because the structural guard is constitutionally unable to see a refusal
  keyed on that constant hoisted above the `code_recorded` split: the
  reference lands inside the function the mapping already pins as its sole
  consumer, so no spelling of that recurrence changes the mapping.

The coverage obligation is bounded to what these two guards can decide: a
second refusal list introduced as a module-level constant, or as an extra
consumer of an existing one, is caught structurally on any spelling; a
second refusal list consulted inside the downstream recorded-code leg is
caught behaviourally — whether it is a module constant or a function-local
literal — when it turns on a code that lies on the guard's pinned code axis;
a dependency on a code outside that axis is not caught, and widening the
axis is the way to buy more. A refusal list that is neither a module-level constant of this
module nor consulted on that leg — for example a function-local literal on
the raw-manifest or model-package leg, or a list living in another module —
is outside this requirement. Two further bounds are stated rather than
closed. First, the run-time cross-check closes installs under a name the
module body does not bind and rebinds that change a name's kind; a
reflective rebind of an already-inventoried constant to a same-kind value
(the name reaching the module object only as a string literal, so no
syntactic reference exists to inventory) is closed only by the constant-value
assertions the guard pins, and those name five of the module's eighteen
module-level constants. The `global` spelling of that same rebind is caught,
because its assignment target is a syntactic reference and therefore changes
the constant's consumer set. Second, the accept-set's catch-all refuses any
statement form the language grows next, but it cannot refuse a form the
interpreter rejects before parsing completes. Source-text literal comparison against
production source SHALL NOT be used to discharge this obligation: the
retired guard's scan admitted a re-added blacklist written on one line, at a
different indent, under a different name, or reordered so that neither
pinned adjacent pair survived.

The `code_recorded=False` domain is explicitly excluded from the
constant-verdict assertion: that branch's classifier refusal list is its
legitimate single source, preserved verbatim under the placeholder ruling.

The structural guard's friction is deliberate and SHALL be treated as part
of the contract rather than as a defect: a legitimate addition or rename of
a module-level constant also fails the guard, once, until the pinned
expected mapping is updated in the same change. A guard that stayed green
for legitimate additions could not fail for a renamed refusal list either —
they are structurally the same event.

#### Scenario: a re-added refusal list fails the suite regardless of spelling

- **WHEN** a second permanent-code refusal list is added to
  `scheduler_state_failure.py` as a module-level constant — on one line, at
  any indent, in any element order, under any name, as a plain or annotated
  assignment — and is consulted by any function
- **THEN** the structural guard fails, because the constant-to-consumer
  mapping no longer equals the pinned mapping
- **AND** when the list is instead introduced through a statement form
  outside the accept-set — measured examples: a tuple-unpacking target, an
  assignment nested in a conditional, loop, `with`, `try` or `match` body, an
  augmented assignment, an attribute of a class other than the one allowed by
  name, and on Python 3.12+ a PEP 695 `type` alias — the guard fails by
  refusing that form, not by a mapping difference; each of those forms leaves
  the mapping byte-identical, so silently passing over them would admit the
  recurrence
- **AND** when the list is installed at run time under a name the module body
  does not bind — the measured example being a `global` assignment executed by
  a module-level call — the guard fails on the source-versus-module
  cross-check, which is the only one of the three mechanisms that can see it:
  the syntactic subject neither inventories such a name nor refuses it, and
  consuming it changes no consumer set
- **AND** when the run-time install instead rebinds a name the module body
  already binds as a function or a class, the same cross-check fails on that
  name's kind, since the inventoried `def` no longer resolves to a function
  on the imported module object

#### Scenario: an existing refusal list gaining a second consumer fails the suite

- **WHEN** an existing remedy refusal constant is consulted by a second
  function in addition to the shared judgement
- **THEN** the structural guard fails on that constant's consumer set, which
  is the acceptance line's actual invariant — one consulted judgement source,
  not one list

#### Scenario: a code-keyed second refusal on the downstream leg fails the suite

- **WHEN** the downstream recorded-code leg's verdict is made to depend on a
  failure code that lies on the guard's pinned code axis — which includes at
  minimum three of the five codes the retired downstream blacklist carried —
  by any construct, including a function-local literal set and either
  spelling of the code key the leg could read
- **THEN** the behavioural guard fails, because the verdict is no longer
  constant across that axis for fixed `permanent` and `limit_exhausted`
- **AND** a dependency introduced on a code outside the pinned axis is
  outside this scenario: the axis bounds what the guard can decide, and
  widening it is the way to buy more

#### Scenario: a classifier-keyed second refusal on the downstream leg fails the suite

- **WHEN** the downstream recorded-code leg's verdict is made to depend on
  the failure classifier — the measured example being the
  `code_recorded=False` leg's own placeholder refusal list hoisted above the
  split, which is the shape an ordinary "check the classifier once at the
  top" simplification produces
- **THEN** the behavioural guard fails on the classifier axis, because the
  verdict is no longer constant across it for fixed `permanent` and
  `limit_exhausted`
- **AND** the structural guard does not fail and cannot be made to: the
  recurrence references a constant the pinned mapping already attributes to
  that same function, so the mapping stays byte-identical on every spelling —
  which is why the classifier axis, not a structural refinement, is the only
  oracle available for this shape

#### Scenario: reformatting production source keeps the suite green

- **WHEN** `scheduler_state_failure.py` is reformatted without semantic
  change
- **THEN** both guards stay green, so neither a false red nor a false green
  can be produced by a line-wrapping or indentation change alone

### Requirement: The repaired-stage-evidence predicate's admitting shapes MUST have regression coverage

Both admitting arms of `_pipeline_job_is_repaired_stage_evidence`, and the absence of both fields, SHALL be pinned by direct predicate coverage on both module copies.

The two arms are a `repair_status` of `repaired` and an `active_blocker` that
is exactly `False`; the two copies carrying the predicate verbatim are
`scheduler_state_rows` and `chain_source_cycle`.

The `active_blocker` arm's coverage SHALL include a row carrying
`active_blocker: False` and **no** `repair_status` (a shape neither of the
two annotating writers emits as of this change — `chain_repository_state`
and `chain_source_cycle` each set the two fields in one payload — so the
arm's discriminating domain is rows read back from persisted state; the
marker write face separately lacks both target keys, tracked as #1482, and a
future writer emitting only one of them would move this shape into the
in-repository domain without invalidating the coverage), and a row carrying
neither field. Together these two rows refuse
both a deletion of the arm and a widening of it to a truthy test: the
truthy form would admit an ordinary failure row that merely lacks the field,
silently converting live failures into repaired ones.

For the `scheduler_state_rows` copy, the coverage SHALL also assert the
downstream `_job_row_is_live_failure` verdict for the same rows, since that
predicate is where the admission decides whether a failed row remains a
repair target.

#### Scenario: deleting the active_blocker arm fails the suite

- **WHEN** the `active_blocker is False` arm is removed from either copy of
  the predicate
- **THEN** the row carrying `active_blocker: False` with no `repair_status`
  fails, on the copy that was changed

#### Scenario: widening the active_blocker arm to a truthy test fails the suite

- **WHEN** the arm is rewritten as a truthy test of the field
- **THEN** the row carrying neither field fails, because a missing field
  would then be read as repaired evidence
