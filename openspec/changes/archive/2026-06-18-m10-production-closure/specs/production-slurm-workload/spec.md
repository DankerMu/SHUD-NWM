## ADDED Requirements

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
- **THEN** downstream frequency, tile, and API publication are blocked for that task with stable error metadata
- **AND** successful sibling task outputs remain intact and publishable
- **AND** no evidence bundle marks the malformed task as a successful publication

### Requirement: Solver runtime resources are explicit

The system SHALL bind SHUD runtime resource settings to model/run manifests instead of implicit shell defaults.

#### Scenario: Runtime thread and resource settings are captured

- **WHEN** a real SHUD workload is submitted
- **THEN** `cpus_per_task`, `SHUD_THREADS`, walltime, partition, memory request, solver binary/module, and working directory are captured in manifest or evidence
- **AND** secret values and credentials are absent from all captured logs and manifests
