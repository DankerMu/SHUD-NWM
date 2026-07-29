# cross-cycle-warm-start-chaining — delta for scheduler-completion-verdict-absence-tolerance

## ADDED Requirements

### Requirement: Cycle completion verdict SHALL tolerate init-state record absence when successor states prove continuity

When a cycle's candidate reaches a terminal-success decision, its successor state is ready (present in the state snapshot index and usable), and the shared init-state comparison returns `absent`, the cycle completion verdict SHALL be `complete`. When the comparison returns `conflict`, the verdict SHALL remain `gap`. When successor state is not ready or not usable, the verdict SHALL remain `gap` regardless of the comparison result. Legacy per-basin terminal rows that record init-state identity SHALL keep their current match semantics unchanged.

#### Scenario: Absence with proven continuity completes the cycle

- **WHEN** a cycle's cohort terminal rows are succeeded, its successor state entries exist in the index with `usable_flag=True`, and the terminal rows record no init-state identity
- **THEN** the cycle completion verdict is `complete` and the next backfill pass admits the successor cycle as the oldest gap

#### Scenario: Conflict still gaps

- **WHEN** the terminal row records an init-state identity that conflicts with the strict warm-start resolution
- **THEN** the verdict remains `gap` and no strictness is relaxed

#### Scenario: Missing successor states still gap

- **WHEN** the terminal decision is success and the init-state record is absent but the successor state entries are missing or not usable
- **THEN** the verdict remains `gap`

#### Scenario: Successor evidence "no verdict" is not proof

- **WHEN** the successor-state evaluation returns no evidence at all (the third state: not evaluated, e.g. outside the strict window)
- **THEN** the absence-tolerant branch does not engage and the verdict remains `gap`

#### Scenario: The downstream journal predecessor identity gate is unchanged

- **WHEN** a cycle's verdict becomes `complete` under absence tolerance and discovery proceeds to the journal predecessor identity check
- **THEN** that gate's behavior is byte-identical to today and successor admission still requires it to pass

#### Scenario: Legacy recorded rows are unaffected

- **WHEN** a cycle's terminal rows are legacy per-basin rows carrying `init_state_id` that matches the strict resolution
- **THEN** the verdict is `complete` exactly as before this change
