# Spec Delta: cross-cycle-warm-start-chaining

## ADDED Requirements

### Requirement: Cycle completion scope SHALL exclude a model for cycles that predate its own state lineage cutover

The cycle completion verdict SHALL exclude from its scope any model that
acquired its state by a recorded clone from a predecessor model
(`cloned_from_model_id` present on a clone row for that `(model_id, source_id)`
pair), for every cycle whose cycle time is strictly earlier than the clone
row's `valid_time` (`t*`). Such a model SHALL likewise not be admitted into the
execution cohort for those cycles. For cycles at or after `t*` the model SHALL be scored and admitted
exactly as any other model.

`t*` SHALL be resolved per `(model_id, source_id)` — clone rows are written per
source and different sources MAY cut over at different instants. `t*` SHALL be
the model's **own** cutover: the earliest clone row by `(valid_time,
created_at)` written under that same `model_id`, which is the instant the
identity came into existence for that source. Both `clone_gate_kind` values —
`'hydrologic_core'` and `'state_compatibility'` — SHALL confer lineage, and a
clone row whose `clone_gate_kind` is absent or unrecognised SHALL still confer
lineage when `cloned_from_model_id` is present.

Lineage resolution SHALL NOT walk the ancestry chain. For a basin recalibrated
more than once (`M → M'` at `t1`, `M' → M''` at `t2`), the boundary for `M''`
SHALL be `t2` and never `t1`: the cycles in `[t1, t2)` were run by `M'`, `M''`
has no history there, and admitting `M''` into their scope would reproduce the
unclosable gap this requirement exists to prevent.

A model with no clone row for the pair SHALL be scored and admitted exactly as
today, with no change to its history-existence signal or to its first-cycle /
cold-start admission path.

This requirement governs completion scope and cohort membership only. The
generation quarantine on the admission side, the strict warm-start lineage
checks, and the derivation of the content-addressed `model_id` are unchanged.

#### Scenario: A recalibrated model does not gap the cycles that predate it

- **WHEN** a model `M'` carries a clone row for `(M', source)` with
  `cloned_from_model_id = M` and `valid_time = t*`, and the completion verdict
  is computed for a cycle whose cycle time is earlier than `t*`
- **THEN** `M'` is absent from the completion scope for that cycle
- **THEN** the cycle's verdict is decided by the remaining in-scope models
  alone, and is `complete` when they all completed it
- **THEN** `M'` is not admitted into that cycle's execution cohort.

#### Scenario: The cutover cycle itself is scored normally

- **WHEN** the completion verdict is computed for the cycle whose cycle time
  equals `t*`
- **THEN** `M'` is in completion scope and the cycle is `complete` only if `M'`
  genuinely completed its pipeline for it
- **THEN** `M'` is admitted into that cycle's cohort and warm-starts from the
  clone row at `valid_time == t*`.

#### Scenario: A model without lineage is unaffected

- **WHEN** the completion verdict is computed for a model that has no clone row
  for the `(model_id, source_id)` pair
- **THEN** the model is in completion scope for every cycle in the window,
  exactly as before this change
- **THEN** its history-existence signal and its first-cycle admission decision
  are byte-for-byte those it would have received before this change.

#### Scenario: A twice-recalibrated model is scoped by its own cutover

- **WHEN** a basin was recalibrated twice, yielding clone rows `M → M'` at `t1`
  and `M' → M''` at `t2` with `t1 < t2`, only `M''` is in the active model set,
  and the verdict is computed for `M''`
- **THEN** `M''` is scoped out of every cycle earlier than `t2`, including the
  cycles in `[t1, t2)` that `M'` ran
- **THEN** the resolution consults only clone rows written under `M''`'s own
  `model_id` and performs no ancestry walk.

#### Scenario: A backdated re-activation does not retroactively scope out run cycles

- **WHEN** more than one clone row exists under a single `model_id` for a
  source, the earliest at `t_a` and a later one at `t_b > t_a`
- **THEN** the boundary is `t_a`
- **THEN** cycles in `[t_a, t_b)` that the identity actually ran stay in scope
  and are still required to have genuinely completed.

### Requirement: An empty completion scope SHALL distinguish lineage exclusion from missing configuration

The completion verdict SHALL distinguish a scope that is empty because no
models were configured or all were excluded by source scope — which SHALL keep
its existing `gap` verdict as a misconfiguration guard — from a scope that
became empty because every model was excluded by the lineage cutover filter,
which SHALL NOT be a gap.

#### Scenario: Every model lineage-scoped out is not a gap

- **WHEN** every model in a cycle's source scope carries a lineage cutover
  later than that cycle's cycle time
- **THEN** the cycle is not scored as a gap and is not selected for backfill
  execution.

#### Scenario: No configured models remains a gap

- **WHEN** the completion verdict is computed with no models in scope before
  the lineage filter is applied
- **THEN** the verdict is `gap`, unchanged.

### Requirement: Lineage exclusion SHALL be recorded in evidence as an annotation

Each `(model, cycle)` pair excluded by the lineage cutover filter SHALL be
recorded in the pass evidence with a distinct reason naming the exclusion, the
predecessor `model_id`, and the resolved `t*`. The record SHALL be an
annotation: it SHALL NOT be read back as an input to any completion,
admission, or selection decision.

#### Scenario: An operator can tell scoping from genuine completion

- **WHEN** an operator reads the pass evidence for a cycle that scored
  `complete` while a recalibrated model was scoped out of it
- **THEN** the evidence names that model, its predecessor `model_id`, and the
  resolved `t*` under a distinct lineage-exclusion reason
- **THEN** the operator can distinguish this cycle from one where every model
  genuinely completed, without re-deriving the lineage.

### Requirement: The stale-identity breaker SHALL NOT re-engage on lineage-excluded cycles

A cycle SHALL keep its `complete` verdict when that verdict follows from a
recalibrated model being lineage-excluded: the journal predecessor-identity
staleness breaker, which evaluates cycles that otherwise score `complete`,
SHALL NOT re-flip it to `gap` on account of the predecessor model's journal
rows still carrying the predecessor's identity tokens.

#### Scenario: Predecessor journal rows do not re-engage the breaker

- **WHEN** a cycle earlier than `t*` scores `complete` with `M'` lineage-
  excluded, and that cycle's journal still holds `M`'s rows with `M`'s identity
  tokens
- **THEN** the breaker does not engage for that cycle and the verdict stays
  `complete`.

### Requirement: Backfill predecessor emission SHALL NOT prepend a candidate that predates its model's lineage cutover

The backfill lane SHALL NOT emit a predecessor candidate for a model at a
cycle earlier than that model's resolved `t*`, when it prepends a predecessor
ahead of a selected cycle.

#### Scenario: No unrunnable prepend at the cutover boundary

- **WHEN** the cycle at `t*` is selected and the predecessor-emission path is
  consulted for `M'`
- **THEN** no predecessor candidate is emitted for `M'` at the cycle before
  `t*`
- **THEN** `M'`'s warm start for the cycle at `t*` resolves to the clone row at
  `valid_time == t*`.
