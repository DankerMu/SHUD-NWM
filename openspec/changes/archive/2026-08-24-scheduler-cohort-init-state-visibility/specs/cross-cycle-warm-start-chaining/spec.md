## MODIFIED Requirements

### Requirement: Cycle completion verdict SHALL tolerate init-state record absence when successor states prove continuity

The cycle completion verdict SHALL evaluate the candidate's terminal decision BEFORE any
strict-warm-start or successor admission early-return. Warm-start admission answers whether a
run SHOULD BE STARTED; it SHALL NOT veto the finding that a run ALREADY COMPLETED. When the
terminal decision is not a terminal success, the verdict SHALL fall back to today's
strict/successor gating unchanged.

When a terminal-success candidate's strict warm-start resolution is ready and names a state, the
completion verdict SHALL compare that selection against the journal-recorded identity returned by
the repository's optional completed-pipeline full-identity accessor whenever that accessor returns
a mapping; when the accessor is unavailable or returns no mapping, the verdict SHALL retain its
existing terminal `hydro_run` evidence input. The journal accessor SHALL expose the accepted-submit
cohort's recorded per-model terminal identity without widening bounded candidate evidence. If the
shared init-state comparison returns `conflict`, the verdict SHALL remain `gap` even when the
successor state is ready. If it returns `match`, the verdict SHALL be `complete` without requiring
successor evidence. If it returns `absent`, the verdict SHALL be `complete` only when the successor
state is ready (present in the state snapshot index and usable); when successor state is not ready,
not usable, or not evaluated, the verdict SHALL remain `gap`. Legacy per-basin terminal rows,
repositories without the optional accessor, and pre-change cohort rows with no recorded identity
SHALL keep these existing match/absence semantics unchanged.

When the strict warm-start resolution is NOT ready **for a reason that means no predecessor state
exists**, the comparison SHALL return `unverifiable` rather than `conflict`, and the verdict SHALL
be `complete` if and only if the successor state proves continuity — the same physical continuity
standard the `absent` branch already applies. This readiness/reason classification SHALL happen
without consulting the optional full-identity accessor: no strict selection exists to compare, and
an identity-provider failure SHALL NOT veto the terminal-first `unverifiable` verdict.

The not-ready reason SHALL be matched against a closed ALLOWLIST, never a denylist: a reason absent
from the allowlist SHALL classify as `conflict` and keep today's hard gap. The allowlist SHALL
contain only reasons whose meaning is "there is genuinely no predecessor state here", and SHALL
NOT contain any reason that reports something wrong with a state that does exist — a lineage or
checksum mismatch, an unusable or unreadable checkpoint, a missing or unavailable index, or
`state_snapshot_index_prior_checkpoint_missing_after_history` (which means history exists but its
checkpoint is gone: the operator-backfill population, an anomaly rather than an absence). A newly
introduced not-ready reason SHALL therefore default to `conflict` until it is deliberately
admitted. No new leniency is introduced: a successor checkpoint that exists and is usable is
itself proof that the cycle ran to completion and produced a usable state. A `conflict` produced by
an actual field disagreement SHALL remain a hard gap.

#### Scenario: Absence with proven continuity completes the cycle

- **WHEN** a cycle's cohort terminal rows are succeeded, its successor state entries exist in the index with `usable_flag=True`, and the terminal rows record no init-state identity
- **THEN** the cycle completion verdict is `complete` and the next backfill pass admits the successor cycle as the oldest gap

#### Scenario: Cohort recorded identity participates in the completion verdict

- **WHEN** an accepted-submit cohort candidate reaches terminal success with no completed hydro identity, its journal has a current per-model terminal row carrying the reservation-time init-state identity, and the strict warm-start resolution is ready and names a state
- **THEN** the completion verdict compares the full recorded mapping (state id and every optional identity field present on both sides) against the strict selection
- **AND** a conflict keeps the cycle `gap` even when successor evidence is ready
- **AND** a match makes the cycle `complete` even when successor evaluation returns no evidence
- **AND** if the repository has no full-identity accessor or the current journal rows carry no identity, the verdict remains `absent` and uses the unchanged successor-readiness fallback

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

- **WHEN** a cycle's candidate is terminal-success, its strict warm-start resolution is not ready for an allowlisted reason, its successor state entry exists in the index with `usable_flag=True`, and its repository exposes the optional completed-pipeline full-identity accessor
- **THEN** the comparison returns `unverifiable`, the cycle completion verdict is `complete`, and the next backfill pass admits the successor cycle as the oldest gap
- **AND** the full-identity accessor is not invoked because the strict resolution names no state to compare

#### Scenario: Strict-not-ready without proven continuity still gaps

- **WHEN** a cycle's candidate is terminal-success and its strict warm-start resolution is not ready, but no usable successor state entry exists
- **THEN** the verdict remains `gap` — the relaxation is conditioned on physical continuity, never on terminal success alone

#### Scenario: Non-terminal candidates keep today's gating

- **WHEN** a cycle's candidate has no terminal-success decision
- **THEN** the verdict path applies the strict and successor readiness gates exactly as before this change

#### Scenario: Successor evidence "no verdict" is not proof

- **WHEN** the successor-state evaluation returns no evidence at all (the third state: not evaluated, e.g. outside the strict window)
- **THEN** the absence-tolerant branch does not engage and the verdict remains `gap`

#### Scenario: The downstream journal predecessor identity gate is unchanged

- **WHEN** a cycle's verdict becomes `complete` under absence tolerance and discovery proceeds to the journal predecessor identity check
- **THEN** that gate's behavior is byte-identical to today and successor admission still requires it to pass

#### Scenario: Legacy recorded rows are unaffected

- **WHEN** a cycle's terminal rows are legacy per-basin rows carrying `init_state_id` that matches the strict resolution
- **THEN** the verdict is `complete` exactly as before this change
