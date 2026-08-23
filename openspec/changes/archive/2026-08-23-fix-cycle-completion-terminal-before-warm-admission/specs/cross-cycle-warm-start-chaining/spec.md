## MODIFIED Requirements

### Requirement: Cycle completion verdict SHALL tolerate init-state record absence when successor states prove continuity

The cycle completion verdict SHALL evaluate the candidate's terminal decision BEFORE any
strict-warm-start or successor admission early-return. Warm-start admission answers whether a
run SHOULD BE STARTED; it SHALL NOT veto the finding that a run ALREADY COMPLETED. When the
terminal decision is not a terminal success, the verdict SHALL fall back to today's
strict/successor gating unchanged.

When a cycle's candidate reaches a terminal-success decision, its successor state is ready
(present in the state snapshot index and usable), and the shared init-state comparison returns
`absent`, the cycle completion verdict SHALL be `complete`. When the comparison returns
`conflict`, the verdict SHALL remain `gap`. When successor state is not ready or not usable,
the verdict SHALL remain `gap` regardless of the comparison result. Legacy per-basin terminal
rows that record init-state identity SHALL keep their current match semantics unchanged.

When the strict warm-start resolution is NOT ready **for a reason that means no predecessor
state exists**, the comparison SHALL return `unverifiable` rather than `conflict`, and the
verdict SHALL be `complete` if and only if the successor state proves continuity — the same
physical continuity standard the `absent` branch already applies.

The not-ready reason SHALL be matched against a closed ALLOWLIST, never a denylist: a reason
absent from the allowlist SHALL classify as `conflict` and keep today's hard gap. The allowlist
SHALL contain only reasons whose meaning is "there is genuinely no predecessor state here", and
SHALL NOT contain any reason that reports something wrong with a state that does exist — a
lineage or checksum mismatch, an unusable or unreadable checkpoint, a missing or unavailable
index, or `state_snapshot_index_prior_checkpoint_missing_after_history` (which means history
exists but its checkpoint is gone: the operator-backfill population, an anomaly rather than an
absence). A newly introduced not-ready reason SHALL therefore default to `conflict` until it is
deliberately admitted. No new leniency is introduced: a
successor checkpoint that exists and is usable is itself proof that the cycle ran to completion
and produced a usable state. A `conflict` produced by an actual field disagreement SHALL remain
a hard gap.

#### Scenario: Absence with proven continuity completes the cycle

- **WHEN** a cycle's cohort terminal rows are succeeded, its successor state entries exist in the index with `usable_flag=True`, and the terminal rows record no init-state identity
- **THEN** the cycle completion verdict is `complete` and the next backfill pass admits the successor cycle as the oldest gap

#### Scenario: Conflict still gaps

- **WHEN** the terminal row records an init-state identity that conflicts with the strict warm-start resolution
- **THEN** the verdict remains `gap` and no strictness is relaxed

#### Scenario: Missing successor states still gap

- **WHEN** the terminal decision is success and the init-state record is absent but the successor state entries are missing or not usable
- **THEN** the verdict remains `gap`

#### Scenario: Terminal success is evaluated before warm-start admission

- **WHEN** a cycle's candidate has a terminal-success decision in the journal while its strict warm-start resolution is not ready
- **THEN** the verdict path evaluates the terminal decision instead of returning `gap` at the strict early-return, and the candidate is not re-blocked for a run that already finished

#### Scenario: Strict-not-ready with proven continuity completes the cycle

- **WHEN** a cycle's candidate is terminal-success, its strict warm-start resolution is not ready (so no state is named for comparison), and its successor state entry exists in the index with `usable_flag=True`
- **THEN** the comparison returns `unverifiable`, the cycle completion verdict is `complete`, and the next backfill pass admits the successor cycle as the oldest gap

#### Scenario: Strict-not-ready without proven continuity still gaps

- **WHEN** a cycle's candidate is terminal-success and its strict warm-start resolution is not ready, but no usable successor state entry exists
- **THEN** the verdict remains `gap` — the relaxation is conditioned on physical continuity, never on terminal success alone

#### Scenario: Non-terminal candidates keep today's gating

- **WHEN** a cycle's candidate has no terminal-success decision
- **THEN** the verdict path applies the strict and successor readiness gates exactly as before this change

## ADDED Requirements

### Requirement: History-existence SHALL be scoped to state entries at or before the candidate's own cycle time

The §8 history-existence signals SHALL count only usable state-index entries whose `valid_time`
is at or before the candidate's cutoff — the candidate's own cycle time. This governs
`history_exists_any_generation` and its current-generation counterpart alike. An entry at `valid_time == cutoff` (the
exact-predecessor location) SHALL continue to count as history, because it proves the model was
previously exercised. An entry at `valid_time > cutoff` SHALL NOT count: such an entry can only
have been produced by the candidate's own run or by a later cycle, and a run's own output SHALL
NOT be treated as evidence that the run had a predecessor.

This closes the general form of the defect that the lineage-cutover scope-outs address per-case
for recalibrated models: a state entry whose `valid_time` postdates the candidate makes
history-existence true at every cycle, which permanently closes the packaged-IC bootstrap branch
for a model's own first cycle and drives the backward-recursion dead chain in §8.6 predecessor
emission. Models carrying a state-lineage cutover keep their existing scope-out behavior
unchanged; this requirement is what makes newly onboarded models — which carry no cutover —
behave correctly without one.

The exact-predecessor lookup key SHALL be unaffected: it resolves at `valid_time == cutoff`,
which remains inside the scope.

#### Scenario: A model's own first-cycle output does not close its bootstrap branch

- **WHEN** history-existence is computed for a newly onboarded model's first cycle, and the only usable state-index entries for that model and source carry `valid_time` later than the cycle time — the entries that cycle's own run produced
- **THEN** history-existence is false, the packaged-IC bootstrap branch stays open for that cycle, and no exact predecessor is demanded from a cycle that never existed

#### Scenario: An entry at the exact predecessor location still counts as history

- **WHEN** a usable state-index entry exists at `valid_time == cutoff`
- **THEN** history-existence is true, unchanged from before this requirement

#### Scenario: Established models are unaffected

- **WHEN** history-existence is computed for a model whose usable state-index entries begin well before the candidate's cycle time
- **THEN** the signal is true exactly as before, and the candidate's branch selection is unchanged

#### Scenario: Scoping does not disturb the exact-predecessor lookup

- **WHEN** an exact-predecessor entry is present at the expected identity key
- **THEN** it is still found and classified (including the wrong-generation quarantine path) exactly as before, because the expected key's `valid_time` equals the cutoff and remains in scope

#### Scenario: A not-ready reason outside the allowlist keeps the hard gap

- **WHEN** a cycle's candidate is terminal-success, its successor state entry is usable, and its strict warm-start resolution is not ready for a reason reporting a defect in an existing state — a lineage or checksum mismatch, an unusable or unreadable checkpoint, or a prior checkpoint missing after history
- **THEN** the comparison returns `conflict`, the verdict remains `gap`, and the successor-continuity tolerance is not applied

#### Scenario: The allowlist is closed against new reasons

- **WHEN** the strict warm-start resolution is not ready for a reason that is not named in the allowlist, whatever that reason is
- **THEN** the comparison returns `conflict` — admission to the relaxation is by explicit enumeration, never by failing to exclude
