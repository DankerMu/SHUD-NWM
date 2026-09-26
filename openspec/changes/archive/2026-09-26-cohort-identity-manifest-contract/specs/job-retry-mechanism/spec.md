## MODIFIED Requirements

### Requirement: Cycle-stage retry minting SHALL derive from the budgeted stage attempt across run_id prefixes

Every scheduler retry decision whose budget reads the `forecast` stage-scoped attempt — the strict warm-start terminal mismatch retry and the ordinary failure retry (`retry_failed_candidate` and the strict-lane escalation built from it) when the failed stage is `forecast` — SHALL carry that attempt to the chain as an explicit `retry_attempt_floor` in its decision evidence. Cohort-shared cycle stages (`convert`, `forcing`) carry no floor. When the chain mints a retry job id for a cycle stage, the attempt suffix SHALL be at least one greater than the stage-scoped retry attempt the scheduler's budget read has charged for that candidate. That budget attempt is derived from all candidate-authoritative rows regardless of their `run_id` prefix. The suffix SHALL also be at least the next attempt of the same prefix's own history. When the chosen id is already present in the journal, the minter SHALL keep advancing to the next free suffix instead of reusing or pinning the occupied id. The budget floor SHALL be carried explicitly to the minter and SHALL NOT be conveyed through the chain context's pinned retry attempt. A `run_id` prefix change — single-model `..._full_<model>` to `..._forecast_<model>`, or a cohort digest change — SHALL therefore not reset the charged attempt, and the budget SHALL demote the candidate on the pass where its cumulative attempt reaches the retry limit. A candidate that has never retried therefore mints `_retry_1` rather than the bare id on its first budgeted retry. When one cohort master carries members with different floors, the master id SHALL be minted above the largest member floor, and the master SHALL record each floored member's floor (`retry_attempt_floors`) at reservation, first-write frozen. Each member's per-model reconciled row SHALL then record its own charged attempt: `min(master effective attempt, own base + (master effective attempt - (largest recorded floor + 1)))`, where the own base is `own floor + 1` for a member with a recorded floor. A member without a recorded floor (a forecast restart that carries no budget floor, such as a manifest-missing or quarantine rerun, which may already have forecast history) SHALL be charged the shared attempt, fail-closed. A nested retry master of a partially failed cohort array, whose active members were narrowed to the failed subset, SHALL record the floors of the master it retries, so the charge base stays the one its id was minted from. For a floored member, the added difference carries every further attempt the master itself consumed (an inline retry of the same call, or an occupied id skipped forward), so a member's charge still advances with each of its submissions. When the master's effective attempt is not greater than the largest recorded floor (an id reused without the floored minter), every member SHALL record the master's effective attempt. A master whose recorded floors are absent or empty (legacy rows, or a cohort whose members carry no floor) SHALL keep the shared charge, every member recording the master's effective attempt; a one-member cohort therefore always records the master's effective attempt, as before. No member is ever charged more than the shared attempt, and the budget still reads the maximum over all of a candidate's authoritative rows, so a lower reconciled charge never forgets an attempt already charged under another row.

#### Scenario: A prefix switch does not reset the minted attempt
- **GIVEN** a candidate that has spent stage-scoped attempt M under the `..._full_<model>` run_id prefix
- **WHEN** a strict warm-start retry is minted under the `..._forecast_<model>` prefix through the public cycle orchestration entry
- **THEN** the minted retry suffix is greater than M
- **AND** the budget emits `blocked_strict_warm_start_init_state_mismatch` on the pass where the cumulative attempt reaches the retry limit, with no further forecast submission

#### Scenario: An occupied id keeps the minter advancing
- **WHEN** the floor-derived retry id already exists as a terminal journal row
- **THEN** the minter selects the next free suffix and the stage is not wedged

#### Scenario: Mixed-floor cohort members are charged their own floor
- **GIVEN** a cohort with member A at floor 0 and member B at floor 2, retry limit 3
- **WHEN** one cohort retry is submitted
- **THEN** the master suffix exceeds both floors, A's next stage-scoped attempt is 1 and B's is 3, so A keeps its remaining budget and B reaches the limit

#### Scenario: A low-floor member is not blocked by a high-floor member
- **GIVEN** retry limit 2, member A at floor 0 and member B at floor 1
- **WHEN** the cohort retry `..._forecast_retry_2` is submitted and reconciled
- **THEN** A's reconciled `retry_count` is 1 and its next decision is a retry, and B's is 2 and its next decision is blocked

#### Scenario: An inline retry of a mixed-floor cohort still advances every member

- **GIVEN** a cohort with member A at floor 0 and member B at floor 1
- **WHEN** `..._forecast_retry_2` fails and the same call mints and submits `..._forecast_retry_3`
- **THEN** after reconcile A's charged attempt is 2 and B's is 3, and no member's total submissions exceed the retry limit

#### Scenario: A partial nested retry keeps the low-floor member's own charge

- **GIVEN** scheduler retry limit 3, an inline retry budget that permits one more submission in the same call, member A at floor 0 and member B at floor 2, and the cohort retry `..._forecast_retry_3`
- **WHEN** only A's task fails and the same call retries the failed subset as `..._forecast_retry_4`
- **THEN** A's charged attempt is 2 (not 4) and its next decision is a retry

#### Scenario: A member without a recorded floor keeps the shared charge

- **GIVEN** a cohort whose master records a floor for member Y only, and member X joins as a forecast restart without a floor
- **WHEN** the master is reconciled
- **THEN** X's reconciled `retry_count` equals the master's effective attempt

#### Scenario: A one-member cohort keeps the master's attempt

- **WHEN** a single-model forecast master (a one-member cohort) is reconciled after an occupied id was skipped forward
- **THEN** the member's reconciled `retry_count` equals the master's effective attempt, identical to the pre-change behavior

#### Scenario: Mixed-floor cohort members are charged the shared attempt
- **GIVEN** a cohort master with member A at floor 0 and member B at floor 2 whose `retry_attempt_floors` is absent or empty (a legacy row)
- **WHEN** it is reconciled
- **THEN** both members' reconciled `retry_count` equals the master's effective attempt, identical to the pre-change behavior, so no member exceeds the retry limit

#### Scenario: An ordinary failure retry does not reset across a prefix switch
- **WHEN** `retry_failed_candidate` retries a candidate whose run_id prefix changed since its earlier attempts
- **THEN** the minted suffix exceeds the attempt already charged and total submissions do not exceed the retry limit

#### Scenario: Cohort digest changes do not escape the budget
- **WHEN** a cohort's membership changes between passes so its `..._forecast_cohort_<digest>` run_id changes
- **THEN** the per-model stage-scoped attempt still advances with each submitted retry and the budget still demotes the candidate at the retry limit
