## ADDED Requirements

### Requirement: Slurm reconcile binds array task identity

The scheduler SHALL reconcile Slurm array task terminal states only when the
terminal Slurm evidence can be bound to the submitted manifest/task identity for
the candidate being updated.

#### Scenario: matching manifest task reconciles success

- **WHEN** `sacct` reports `COMPLETED|0:0` for an array task
- **AND** the submitted manifest, task id, run id, stage, and model id match the
  candidate or runtime stdout evidence
- **THEN** reconcile MUST mark that task/job succeeded and persist the matching
  identity evidence.

#### Scenario: generic job name is insufficient

- **WHEN** Slurm terminal status exists but reconcile can only prove a generic
  job name such as `nhms_forecast` or `nhms_forcing`
- **THEN** reconcile MUST leave the job/task in an unverified state with
  `SLURM_RECONCILE_UNVERIFIED`
- **AND** it MUST NOT fabricate success for the candidate.

### Requirement: Scheduler array budgets constrain Gateway submission

The scheduler SHALL propagate each cohort's share of the global Slurm array
concurrency budget to the Gateway, and the Gateway SHALL enforce the minimum of
that budget, the resource profile limit, and the array task count.

#### Scenario: multiple cohorts share a global bound

- **WHEN** one pass submits cohorts with 14, 18, and 4 eligible tasks under a
  global array concurrency bound of 32
- **THEN** their simultaneous array throttles SHALL be no greater than 14, 14,
  and 4 respectively
- **AND** the Gateway SHALL render each submission as
  `--array=0-(N-1)%min(cohort_budget,profile_limit,N)`.

#### Scenario: malformed scheduler budget is rejected

- **WHEN** an array manifest supplies a non-integer, boolean, zero, or negative
  concurrency budget
- **THEN** the Gateway MUST reject the request before invoking `sbatch`.
