## ADDED Requirements

### Requirement: Real SHUD executable preflight
The production runtime SHALL reject stub executables and validate the configured SHUD binary before submitting work.

#### Scenario: Stub executable rejected
- **WHEN** `SHUD_EXECUTABLE` is `/bin/true`, `/bin/false`, empty, missing, or otherwise configured as a non-SHUD stub
- **THEN** runtime preflight fails with a production-blocking error
- **AND** no Slurm job or successful hydro run is recorded.

#### Scenario: Scheduler preflight blocks stub before submit
- **WHEN** scheduler or orchestrator Slurm mode prepares to submit a SHUD job
- **THEN** it validates `SHUD_EXECUTABLE` and required runtime paths before calling the Slurm gateway or `submit_job_array`
- **AND** a stub or missing executable produces no Slurm submission, no active pipeline job, and no hydro success state.

#### Scenario: SHUD binary dependencies validated
- **WHEN** a SHUD executable is configured
- **THEN** preflight verifies that the executable exists, is executable, is visible from the node-22 execution context, has required shared libraries available, and can report a bounded version/help or dry preflight signal
- **AND** missing libraries are reported as blockers without leaking environment secrets.

#### Scenario: Project inputs validated
- **WHEN** SHUD runtime starts for QHH
- **THEN** it verifies required project input files, generated forcing files, workspace paths, and manifest identities before execution
- **AND** failures are recorded as pre-submit blockers.

### Requirement: Node-22 Slurm submission path
SHUD production execution SHALL use a working node-22 Slurm gateway or host submission path with health and accounting receipts.

#### Scenario: Slurm gateway healthy
- **WHEN** scheduler is configured for Slurm execution
- **THEN** preflight verifies the gateway or host service health, submit capability, allowed sbatch template, log root, and account/partition policy before candidate submission
- **AND** evidence records the gateway mode without exposing credentials.

#### Scenario: Slurm unavailable blocks execution
- **WHEN** Slurm CLI/gateway/accounting is unavailable or points to an invalid self-reference
- **THEN** scheduler records a Slurm blocker
- **AND** it does not mark the SHUD stage submitted or succeeded.

#### Scenario: Slurm receipt persisted
- **WHEN** SHUD is submitted to Slurm
- **THEN** `ops.pipeline_job` and/or `ops.pipeline_event` records Slurm job id, array task id when applicable, submit time, status, log URI, and resource/accounting metadata when available
- **AND** repeated scheduler scans do not submit duplicate active jobs.

### Requirement: SHUD execution result state
The runtime SHALL distinguish successful SHUD completion, failed execution, cancelled execution, and missing/unparseable output.

#### Scenario: SHUD completes with output
- **WHEN** Slurm reports successful SHUD completion and expected output files exist
- **THEN** the forecast stage is recorded as completed with output artifact references and checksums
- **AND** parse can proceed for the same run/model/source/cycle identity.

#### Scenario: SHUD fails or output missing
- **WHEN** Slurm reports failure, cancellation, timeout, missing logs, missing outputs, or unparseable runtime status
- **THEN** the pipeline records a typed failure or blocker
- **AND** parse/publish stages do not claim success.
