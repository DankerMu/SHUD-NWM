## MODIFIED Requirements

### Requirement: Cycle completion verdict SHALL tolerate init-state record absence when successor states prove continuity

When a cycle's candidate reaches a terminal-success decision, the completion verdict SHALL compare the strict warm-start selection against the journal-recorded identity returned by the repository's optional completed-pipeline full-identity accessor whenever that accessor returns a mapping; when the accessor is unavailable or returns no mapping, the verdict SHALL retain its existing terminal `hydro_run` evidence input. The journal accessor SHALL expose the accepted-submit cohort's recorded per-model terminal identity without widening bounded candidate evidence. If the shared init-state comparison returns `conflict`, the verdict SHALL remain `gap` even when the successor state is ready. If it returns `match`, the verdict SHALL be `complete` without requiring successor evidence. If it returns `absent`, the verdict SHALL be `complete` only when the successor state is ready (present in the state snapshot index and usable); when successor state is not ready, not usable, or not evaluated, the verdict SHALL remain `gap`. Legacy per-basin terminal rows, repositories without the optional accessor, and pre-change cohort rows with no recorded identity SHALL keep these existing match/absence semantics unchanged.

#### Scenario: Absence with proven continuity completes the cycle

- **WHEN** a cycle's cohort terminal rows are succeeded, its successor state entries exist in the index with `usable_flag=True`, and the terminal rows record no init-state identity
- **THEN** the cycle completion verdict is `complete` and the next backfill pass admits the successor cycle as the oldest gap

#### Scenario: Cohort recorded identity participates in the completion verdict

- **WHEN** an accepted-submit cohort candidate reaches terminal success with no completed hydro identity, its journal has a current per-model terminal row carrying the reservation-time init-state identity, and the strict warm-start resolution is available
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

#### Scenario: Successor evidence "no verdict" is not proof

- **WHEN** the successor-state evaluation returns no evidence at all (the third state: not evaluated, e.g. outside the strict window)
- **THEN** the absence-tolerant branch does not engage and the verdict remains `gap`

#### Scenario: The downstream journal predecessor identity gate is unchanged

- **WHEN** a cycle's verdict becomes `complete` under absence tolerance and discovery proceeds to the journal predecessor identity check
- **THEN** that gate's behavior is byte-identical to today and successor admission still requires it to pass

#### Scenario: Legacy recorded rows are unaffected

- **WHEN** a cycle's terminal rows are legacy per-basin rows carrying `init_state_id` that matches the strict resolution
- **THEN** the verdict is `complete` exactly as before this change
