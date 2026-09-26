## ADDED Requirements

### Requirement: Chain stage polling SHALL NOT let a transient gateway or persistence failure silently end a unit's chain, nor resubmit a bound job

Inside the chain stage poll loop:

- A Slurm gateway status query failure SHALL be retried until the existing job deadline.
- If the deadline is reached after at least one query failure, the stage SHALL end, for every stage, with a governed non-resubmitting result `reconcile_unverified` / `SLURM_STATUS_QUERY_UNAVAILABLE`. It SHALL carry the failure count, SHALL write no terminal status, and SHALL leave the Slurm id bound.
- A deadline reached without query failures SHALL keep the existing timeout behavior.
- A pipeline-job runtime status write failure, other than the governed accepted-submit conflict, SHALL end that unit's stage with `reconcile_unverified` / `STAGE_RUNTIME_STATUS_PERSIST_FAILED`. The callers SHALL then perform no further repository write or gateway call for that stage and SHALL NOT schedule a resubmission. The unit's members SHALL be recorded as reconciling, and the next pass SHALL resolve the still-bound job.
- A pipeline event write failure SHALL be recorded and SHALL NOT end the chain.

The stage timing span SHALL record the real `basin_count` on every exit path.

#### Scenario: Transient gateway query failures are retried in-loop

- **WHEN** the gateway status query raises twice and then reports the job terminal
- **THEN** the stage completes normally, no exception escapes `orchestrate_cycle`, and the unit's downstream stages run in the same pass

#### Scenario: A gateway outage until the deadline does not resubmit

- **WHEN** a convert stage's gateway status query raises on every poll until the deadline
- **THEN** the stage ends `reconcile_unverified` with `SLURM_STATUS_QUERY_UNAVAILABLE`, no terminal status is written, the Slurm id stays bound, and the gateway submit count does not increase

#### Scenario: A status persistence failure is governed and resolved next pass

- **WHEN** the runtime status write raises a non-conflict error while polling a bound Slurm job and every later write of that stage also raises
- **THEN** the stage ends `reconcile_unverified` with `STAGE_RUNTIME_STATUS_PERSIST_FAILED` without further writes, the members are reconciling rather than `submission_failed`, the span `basin_count` equals the entry basin count, other units are unaffected, and on the next pass with healthy persistence the row reaches its real terminal status and downstream stages continue with no additional submission

#### Scenario: An event write failure does not end the chain

- **WHEN** the pipeline event write raises once after a successful status write
- **THEN** polling continues to the terminal status, the unit's chain proceeds, and the failure is counted in evidence

### Requirement: A model-less cohort row SHALL be attributed by recorded cohort membership

Each model-less cycle-scope cohort row visible to a candidate SHALL be classified from the cycle's recorded `cohort_members`, using the same completeness rule as active-pipeline detection. The classification SHALL be made before the candidate state is compacted. A row that records its own `cohort_members` SHALL be judged by its own list, and only other rows by the run's union. It SHALL have four values:

- `member`: the run's complete recorded membership contains the candidate.
- `non_member`: the run's complete recorded membership does not contain the candidate.
- `incomplete`: the run's recorded membership is truncated or invalid.
- `unwitnessed`: no row of the run records membership.

`member` rows SHALL count as the candidate's own on both sides:

- the failure side: latest failure, permanence, retry count, identity filtering, attempt floor, and decision authority;
- the success side.

`non_member` rows SHALL affect neither side. A permanently failed `incomplete` row SHALL block the candidates it is visible to with `cohort_membership_unprovable`. `unwitnessed` rows SHALL keep their existing semantics. A candidate with its own terminal success for the cycle SHALL NOT be blocked by `cohort_membership_unprovable`, and a manual-retry marker SHALL clear that block as it clears the permanent-failure guard. The downstream-stage member record SHALL add only `cohort_members`, SHALL NOT add any accepted-submit master marker, and SHALL be limited to repositories supporting accepted-submit reconcile.

Every model-less cohort master row of a stage downstream of forecast SHALL record its members at creation.

#### Scenario: Multi-member cohort permanent state_save_qc failure blocks members

- **WHEN** a three-member cohort's `state_save_qc` exhausts its retries and is marked `permanently_failed`
- **THEN** each member candidate decision is `blocked` by `permanent_failure_guard` with `failure.stage=state_save_qc` and `automatic_retry_allowed=false`

#### Scenario: Transient cohort failures consume the retry budget across passes

- **WHEN** the same cohort's `state_save_qc` fails transiently in two or more consecutive passes
- **THEN** each member's attempt increments with the cohort row's retry count, and automatic retry stops when the budget is exhausted

#### Scenario: A strict-subset restart cohort is witnessed by its own rows

- **WHEN** a `state_save_qc` restart cohort containing only some of the original members is created, and it later succeeds or fails permanently
- **THEN** its rows record their members, its success is credited to exactly those members without resubmission, and its permanent failure blocks exactly those members

#### Scenario: Siblings, incomplete and unwitnessed rows

- **WHEN** a sibling cohort of the same cycle without the candidate fails or succeeds
- **THEN** that cohort's rows do not change the candidate's decision
- **WHEN** a permanently failed cohort row's recorded membership is incomplete
- **THEN** the candidates it is visible to are blocked with `cohort_membership_unprovable`
- **WHEN** a historical cohort run records no membership at all
- **THEN** decisions are identical to the pre-change behavior

### Requirement: Failed or unverified forecast cohort task projections SHALL restart from forecast

The forecast cohort terminal projection SHALL set `restart_stage` to `forecast` for every failed, cancelled, or unverified array task. It SHALL do so regardless of the basin's cohort-entry restart stage, and SHALL keep `state_save_qc` for succeeded tasks.

#### Scenario: Convert- or forcing-restarted cohort with a failed forecast task

- **WHEN** a file-journal forecast cohort whose basins carry `restart_stage` `convert` or `forcing` ends with one failed and one missing array task
- **THEN** the per-task projection is persisted with `restart_stage=forecast` and the cohort is not deferred as `identity_mismatch_blocked`
