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
