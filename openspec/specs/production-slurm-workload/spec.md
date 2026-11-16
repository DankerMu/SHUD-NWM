# production-slurm-workload Specification

## Purpose
TBD - created by archiving change m10-production-closure. Update Purpose after archive.

## Requirements

### Requirement: Real Slurm production workload is verifiable

The system SHALL provide an opt-in validation lane that runs a Basins-backed SHUD workload through the real Slurm cluster and records reproducible evidence.

#### Scenario: Real Slurm workload completes with shared logs

- **WHEN** production closure validation is enabled on a host with Slurm CLI access and a Basins-backed model package is available
- **THEN** the system submits a manifest-driven SHUD job to the configured partition/account
- **AND** stdout/stderr are written to shared storage rather than compute-node-local `/tmp`
- **AND** `sacct` evidence records job ID, state, exit code, elapsed time, node list, and partition
- **AND** the evidence bundle links the Slurm job to run_id, model_id, model version, workspace, and object-store artifact URIs

### Requirement: Slurm arrays support partial success and retry

The system SHALL prove that multi-model Slurm arrays can continue when one model fails and can retry or cancel individual failed work without corrupting successful outputs.

#### Scenario: Controlled array failure does not block successful models

- **WHEN** a Slurm array includes at least one successful task and one controlled failing task
- **THEN** successful task outputs remain publishable
- **AND** failed task metadata includes error code, stderr path, retry count, and failure stage
- **AND** retry/cancel behavior is visible through monitoring or persisted DB evidence

#### Scenario: Malformed SHUD output blocks only the affected task

- **WHEN** a Slurm array task completes but its SHUD output is malformed, contains NaN/Inf values, is missing a required output file, or has count/time mismatches
- **THEN** downstream parse, tile, and API publication are blocked for that task with stable error metadata
- **AND** successful sibling task outputs remain intact and publishable
- **AND** no evidence bundle marks the malformed task as a successful publication

### Requirement: Solver runtime resources are explicit

The system SHALL bind SHUD runtime resource settings to model/run manifests instead of implicit shell defaults.

#### Scenario: Runtime thread and resource settings are captured

- **WHEN** a real SHUD workload is submitted
- **THEN** `cpus_per_task`, `SHUD_THREADS`, walltime, partition, memory request, solver binary/module, and working directory are captured in manifest or evidence
- **AND** secret values and credentials are absent from all captured logs and manifests

### Requirement: Production-closure render uses the submission's own resource profile

The production-closure Slurm validation SHALL render the sbatch script from the resource profile of the current submission held in memory, with the deployment-level partition and exclude-node overrides applied by the same resolution function the gateway uses. The `lane_dir/resource_profiles.yaml` file SHALL remain byte-identical evidence and SHALL NOT be read back as a render input.

#### Scenario: Concurrent forced submission overwrites the lane profile before render

- **WHEN** two `validate-slurm --submit --force` runs share a `run_id` with different resource env, and B overwrites `lane_dir/resource_profiles.yaml` after A wrote it but before A renders
- **THEN** A's rendered script MUST carry A's partition, memory, walltime, cpus-per-task and SHUD thread values

#### Scenario: Deployment partition override still applies

- **WHEN** `SLURM_GATEWAY_PARTITION_OVERRIDE` and `SLURM_GATEWAY_EXCLUDE_NODES` are set in the process environment
- **THEN** the rendered script MUST use the override partition and exclude-node values

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
