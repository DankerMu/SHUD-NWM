## ADDED Requirements

### Requirement: Cycle-stage retry minting SHALL derive from the budgeted stage attempt across run_id prefixes

Every scheduler retry decision whose budget reads the `forecast` stage-scoped attempt — the strict warm-start terminal mismatch retry and the ordinary failure retry (`retry_failed_candidate` and the strict-lane escalation built from it) when the failed stage is `forecast` — SHALL carry that attempt to the chain as an explicit `retry_attempt_floor` in its decision evidence. Cohort-shared cycle stages (`convert`, `forcing`) carry no floor. When the chain mints a retry job id for a cycle stage, the attempt suffix SHALL be at least one greater than the stage-scoped retry attempt the scheduler's budget read has charged for that candidate. That budget attempt is derived from all candidate-authoritative rows regardless of their `run_id` prefix. The suffix SHALL also be at least the next attempt of the same prefix's own history. When the chosen id is already present in the journal, the minter SHALL keep advancing to the next free suffix instead of reusing or pinning the occupied id. The budget floor SHALL be carried explicitly to the minter and SHALL NOT be conveyed through the chain context's pinned retry attempt. A `run_id` prefix change — single-model `..._full_<model>` to `..._forecast_<model>`, or a cohort digest change — SHALL therefore not reset the charged attempt, and the budget SHALL demote the candidate on the pass where its cumulative attempt reaches the retry limit. A candidate that has never retried therefore mints `_retry_1` rather than the bare id on its first budgeted retry. When one cohort master carries members with different floors, the master id SHALL be minted above the largest member floor, and every member's per-model reconciled row SHALL record the master's effective attempt. A lower-floor member is therefore charged the shared attempt and can only reach the retry limit earlier, never exceed it. Charging each member only its own floor is deferred to a follow-up.

#### Scenario: A prefix switch does not reset the minted attempt
- **GIVEN** a candidate that has spent stage-scoped attempt M under the `..._full_<model>` run_id prefix
- **WHEN** a strict warm-start retry is minted under the `..._forecast_<model>` prefix through the public cycle orchestration entry
- **THEN** the minted retry suffix is greater than M
- **AND** the budget emits `blocked_strict_warm_start_init_state_mismatch` on the pass where the cumulative attempt reaches the retry limit, with no further forecast submission

#### Scenario: An occupied id keeps the minter advancing
- **WHEN** the floor-derived retry id already exists as a terminal journal row
- **THEN** the minter selects the next free suffix and the stage is not wedged

#### Scenario: Mixed-floor cohort members are charged the shared attempt
- **GIVEN** a cohort with member A at floor 0 and member B at floor 2
- **WHEN** one cohort retry is submitted
- **THEN** the master suffix exceeds both floors and both members' next stage-scoped attempt equals that suffix, so no member exceeds the retry limit

#### Scenario: An ordinary failure retry does not reset across a prefix switch
- **WHEN** `retry_failed_candidate` retries a candidate whose run_id prefix changed since its earlier attempts
- **THEN** the minted suffix exceeds the attempt already charged and total submissions do not exceed the retry limit

#### Scenario: Cohort digest changes do not escape the budget
- **WHEN** a cohort's membership changes between passes so its `..._forecast_cohort_<digest>` run_id changes
- **THEN** the per-model stage-scoped attempt still advances with each submitted retry and the budget still demotes the candidate at the retry limit
