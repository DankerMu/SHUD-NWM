## MODIFIED Requirements

### Requirement: A non-convergent quarantine SHALL be broken once a quarantine rerun re-records the stale identity

A forecast cohort reservation whose basin carries the `retry_journal_predecessor_identity_mismatch` decision SHALL stamp quarantine-rerun provenance onto the cohort MASTER row (the affected model ids, written by the reservation writer — the §8.7 scoring and filtering surfaces themselves remain strictly read-only). When the stale recorded `init_state_id` for one cycle and model has been re-recorded by at least one qualifying cohort MASTER row carrying that provenance for that model — counted read-only from the journal, never from the bounded candidate-state payload — the scheduler SHALL treat that row as a completed convergence attempt when either the master has aggregate terminal-success status or the master has `partially_failed` status and its bounded `candidate_projections` contains the exact model with `array_task_outcome="succeeded"`. An aggregate terminal-success master SHALL remain countable without `candidate_projections`; a `partially_failed` master whose exact model projection is failed, missing, malformed, or unavailable after the 256-entry bound SHALL NOT count. Per-model terminal rows that reconcile copies the identity onto are excluded, so one submission's master row plus its per-model terminal row count as one. Masters minted by non-quarantine replacements (for example a missing-run-manifest or missing-forecast-output resubmission) carry no such provenance and SHALL NOT arm the breaker; journals written before this change carry no provenance field and SHALL leave the breaker disengaged. Once the count reaches the existing threshold, the scheduler SHALL stop producing the quarantine retry: the candidate-side filter SHALL demote the decision to a blocked decision carrying a typed reason, the recorded and expected identity tokens, the occurrence count, and a manual-retry-required retry policy; the discovery-side backfill selection SHALL exclude a cycle from the single oldest-first execution slot only when every model keeping that cycle a gap is breaker-engaged (a cycle with any genuinely incomplete model SHALL keep taking the slot), while still reporting the excluded cycle as a gap (never as complete) and emitting a not-selected evidence entry that carries both identity tokens. No journal row SHALL be written or deleted, and an unavailable or failed occurrence count SHALL leave the breaker disengaged and the quarantine retry decision unchanged (fail toward liveness).

#### Scenario: Breaker demotes the quarantine to blocked after a provenance-stamped rerun re-records the token

- **WHEN** a completed quarantine rerun (its master row stamped with quarantine-rerun provenance for the model) has re-recorded the same stale `init_state_id` that the current positive mismatch observes
- **THEN** the candidate-side decision is the blocked decision with the recorded token, the expected token, the provenance-stamped occurrence count, and `manual_retry_required` true — while before any stamped rerun completes the decision stays the quarantine retry

#### Scenario: Partially failed cohort counts the quarantined model that succeeded

- **WHEN** a provenance-stamped master is `partially_failed`, records the stale token for the target model, and its bounded projection for that exact model has `array_task_outcome="succeeded"`
- **THEN** the occurrence count includes that master exactly once, regardless of another cohort member's failed task

#### Scenario: Partially failed cohort does not count a failed or unavailable target model

- **WHEN** a provenance-stamped master is `partially_failed` but the target model's projection says `failed`, is absent, is malformed, or is unavailable after bounded projection truncation
- **THEN** the master contributes zero occurrences for that model and the breaker remains fail-toward-liveness

#### Scenario: Aggregate-success legacy shape remains countable without projections

- **WHEN** a provenance-stamped aggregate terminal-success master records the stale token but has no `candidate_projections` field
- **THEN** it contributes one occurrence exactly as before this change

#### Scenario: Non-quarantine replacements do not pre-arm the breaker

- **WHEN** the journal holds two or more qualifying masters recording the same stale token but none carries quarantine-rerun provenance for the model (for example the second master came from a missing-run-manifest or missing-forecast-output replacement)
- **THEN** the first quarantine judgement is the ordinary quarantine retry, and the convergence layer gets its rerun before any fail-stop

#### Scenario: Pre-change journals leave the breaker disengaged

- **WHEN** the cycle's journal rows were written before this change and carry no provenance field
- **THEN** the breaker stays disengaged and the quarantine retry behavior is unchanged

#### Scenario: Breaker-engaged gap releases the backfill execution slot

- **WHEN** the oldest available incomplete cycle is breaker-engaged and a later available gap exists for the same source
- **THEN** the later gap is selected for execution, the breaker-engaged cycle appears as a not-selected evidence entry carrying both identity tokens, and its completion status remains gap

#### Scenario: One submission's master and terminal rows count as one

- **WHEN** a single qualifying provenance-stamped quarantine rerun has written the stale token onto its cohort master row and reconcile has copied the identity onto the model's per-model terminal row
- **THEN** the provenance-stamped occurrence count for that token is exactly 1 — the reconcile copy is never double-counted — and, that count witnessing one failed convergence attempt, the breaker engages; the same two rows without the provenance stamp count 0 and leave the quarantine retry unchanged

#### Scenario: Mixed cycle with a genuinely incomplete model keeps the slot

- **WHEN** one model of cycle T is breaker-engaged while another model of the same cycle still has work the scheduler could progress — whether it has no completed pipeline at all, or has its own positive identity mismatch whose provenance-stamped count has not engaged the breaker
- **THEN** cycle T still takes the backfill execution slot and executes normally for the model that is not breaker-engaged

#### Scenario: Unavailable occurrence count leaves the breaker disengaged

- **WHEN** the journal occurrence count cannot be read (no accessor on the repository, no rows, or unreadable rows)
- **THEN** the breaker stays disengaged and the candidate-side decision remains `retry_journal_predecessor_identity_mismatch`

#### Scenario: Journal remains untouched by the breaker

- **WHEN** the breaker engages for cycle T
- **THEN** the journal's on-disk content is byte-identical after the pass
