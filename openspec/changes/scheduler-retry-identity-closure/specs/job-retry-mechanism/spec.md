## ADDED Requirements

### Requirement: Candidate-aware attempt reads SHALL apply one authority domain to rows and carried floors

When a scheduler decision has candidate identity and reads a canonical stage attempt from a raw cycle-wide candidate-state projection, the system SHALL apply the same candidate-authority predicate to both the in-window `pipeline_jobs` row scan and the carried `stage_retry_attempt_floors` contributors before spending the candidate's retry budget. A bare source-cycle run row authoritative for all candidates in that cycle SHALL remain eligible, while a suffixed execution-cohort row or foreign-model row that proves no candidate authority SHALL contribute neither its row attempt nor its carried floor. The scoped read SHALL be copy-on-read and SHALL NOT mutate the raw projected state. Candidate-agnostic consumers and the flat top-level `retry_count` channel remain unchanged.

#### Scenario: In-window execution cohort does not spend candidate budget

- **WHEN** a strict-warm-start terminal mismatch is evaluated with an in-window model-less canonical-stage row whose suffixed execution-cohort run id carries an attempt at or above the retry limit but proves no authority for the candidate
- **THEN** that row contributes no attempt, the decision remains `retry_strict_warm_start_terminal_init_state_mismatch`, and the caller's raw state is unchanged

#### Scenario: Bare cycle and candidate-owned attempts still bind

- **WHEN** the same budget reads either a bare `cycle_<source>_<stamp>` stage row authoritative for the cycle or a candidate-owned matching-stage row at or above the retry limit
- **THEN** the attempt still binds and the decision remains the existing `blocked_strict_warm_start_init_state_mismatch` shape with reason `strict_warm_start_retry_budget_exhausted`

#### Scenario: Carried and in-window authority cannot diverge

- **WHEN** the same non-authoritative contributor appears inside the row window in one projection and outside it as a carried floor source in another otherwise-equivalent projection
- **THEN** both projections produce the same candidate-scoped attempt and decision, with neither path charging the contributor

### Requirement: Top-level source-cycle download blockers SHALL bind to blocker-row identity

A shared-cycle aggregate's top-level source-cycle download failure SHALL be recognized and restored only when a concrete unrepaired download blocker row matches the candidate's expected source and cycle through the existing source-cycle identity predicate. The candidate state's top-level `run_id` SHALL NOT be treated as the blocker row's identity. When no matching blocker row is available, or the available row names another source or cycle, the top-level blocker SHALL fail closed and SHALL NOT be restored.

#### Scenario: Real projected blocker reaches the restore branch and narrows carried attempts

- **WHEN** `candidate_state_from_rows` projects an active source-cycle download failure into top-level failure fields while retaining the matching source-cycle blocker row, a non-candidate-authoritative canonical-stage row contributes a carried attempt floor, and candidate identity filtering removes the generic candidate-state source
- **THEN** the blocker predicate is true without replacing the candidate's top-level `run_id`, the restore branch preserves the stable download failure fields, and the shared-cycle aggregate branch keeps the matching blocker evidence while removing the non-authoritative carried floor through its existing narrowing rule

#### Scenario: Foreign blocker row is not restored

- **WHEN** the top-level fields look like a download failure but the concrete blocker row names a different source or cycle, or no blocker row proves the identity
- **THEN** the blocker predicate is false and candidate filtering does not restore those top-level fields

### Requirement: Geometry-B manual retry minting SHALL recover stage from exact marker-floor lineage

When candidate row truncation leaves `_candidate_failed_stage` unresolved but an adopted manual-retry marker targets a row represented by an authoritative `stage_retry_attempt_floor_sources` contributor, only manual retry evidence composition SHALL recover a canonical stage, and only from an exact target-identifier match between that marker and contributor. A recorded marker `failed_stage`, when present, SHALL agree with the contributor stage after both are interpreted through the canonical downstream-stage identity; the floor-source mapping key itself SHALL use the canonical spelling emitted by the projection producer and an alias-spelled or non-canonical hand-shaped key SHALL NOT authorize recovery. The system SHALL use that canonical stage's carried attempt as `previous_attempt` and mint `new_attempt = previous_attempt + 1`; the manual override SHALL remain allowed regardless of the automatic retry limit. Projection visibility SHALL remain unchanged: the failed row stays outside `pipeline_jobs`, top-level `failed_stage`/`stage`/`restart_stage` stay empty, and `_failed_stage` plus `_candidate_failed_stage` remain unresolved. If lineage is absent, foreign, stale, non-canonical, or maps to multiple disagreeing stages, the system SHALL preserve the existing stage-less fallback rather than guess.

#### Scenario: Geometry B mints the next durable attempt without changing visibility

- **WHEN** the failed `forecast` retry row carrying attempt `N` is outside the row window, terminal-success filler leaves all top-level stage keys empty, and the newest adopted marker without an explicit attempt exactly targets that floor contributor
- **THEN** the failed row remains outside the projection, `_failed_stage` and `_candidate_failed_stage` still return no stage, and only manual retry evidence recovers the target stage to report `previous_attempt == N` and `new_attempt == N+1`, preventing reuse of an already-consumed `_retry_1` identity

#### Scenario: Legacy marker can use exact contributor lineage

- **WHEN** an adopted legacy marker lacks recorded `failed_stage` but its exact `entity_id` or recorded previous job id matches a unique canonical-stage floor contributor
- **THEN** the contributor's canonical stage is recovered without parsing stage text from the job id and the mint uses that stage's floor

#### Scenario: Recorded stage aliases agree by canonical identity

- **WHEN** an adopted marker records a valid downstream alias such as `run_shud_forecast`, its exact target matches the producer's canonical `forecast` floor contributor, and no other stage matches
- **THEN** the marker and contributor agree as canonical `forecast`, and manual retry mints from that floor rather than falling back to attempt one

#### Scenario: Ambiguous or foreign lineage does not infer a stage

- **WHEN** an adopted marker has no exact authoritative contributor match, names a foreign or stale contributor, meets an alias-spelled/non-canonical floor-source key, or its target identifiers map to disagreeing canonical stages
- **THEN** no stage is inferred, unrelated floors are not charged, and the pre-change stage-less fallback result is preserved

#### Scenario: Existing manual retry precedence is unchanged

- **WHEN** the candidate failed stage is already nameable or the newest adopted marker explicitly pins a retry attempt
- **THEN** the existing nameable-stage and explicit-marker precedence remains unchanged, including manual override of the automatic retry limit
