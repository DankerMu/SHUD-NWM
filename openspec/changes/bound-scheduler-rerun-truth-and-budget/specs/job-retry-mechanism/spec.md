## ADDED Requirements

### Requirement: Cycle-stage retry minting SHALL derive from the budgeted stage attempt across run_id prefixes

When the chain mints a retry job id for a cycle stage, the attempt suffix SHALL be at least one greater than the stage-scoped retry attempt the scheduler's budget read has charged for that candidate. That budget attempt is derived from all candidate-authoritative rows regardless of their `run_id` prefix. The suffix SHALL also be at least the next attempt of the same prefix's own history. When the chosen id is already present in the journal, the minter SHALL keep advancing to the next free suffix instead of reusing or pinning the occupied id. The budget floor SHALL be carried explicitly to the minter and SHALL NOT be conveyed through the chain context's pinned retry attempt. A `run_id` prefix change — single-model `..._full_<model>` to `..._forecast_<model>`, or a cohort digest change — SHALL therefore not reset the charged attempt, and the budget SHALL demote the candidate on the pass where its cumulative attempt reaches the retry limit.

#### Scenario: A prefix switch does not reset the minted attempt
- **GIVEN** a candidate that has spent stage-scoped attempt M under the `..._full_<model>` run_id prefix
- **WHEN** a strict warm-start retry is minted under the `..._forecast_<model>` prefix through the public cycle orchestration entry
- **THEN** the minted retry suffix is greater than M
- **AND** the budget emits `blocked_strict_warm_start_init_state_mismatch` on the pass where the cumulative attempt reaches the retry limit, with no further forecast submission

#### Scenario: An occupied id keeps the minter advancing
- **WHEN** the floor-derived retry id already exists as a terminal journal row
- **THEN** the minter selects the next free suffix and the stage is not wedged

#### Scenario: Cohort digest changes do not escape the budget
- **WHEN** a cohort's membership changes between passes so its `..._forecast_cohort_<digest>` run_id changes
- **THEN** the per-model stage-scoped attempt still advances with each submitted retry and the budget still demotes the candidate at the retry limit
