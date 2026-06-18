# real-slurm-gateway-contract Specification

## Purpose
TBD - created by archiving change m7-second-review-remediation. Update Purpose after archive.
## Requirements
### Requirement: Structured real Slurm submit contract
Orchestrator and RealSlurmGateway SHALL share one explicit structured submit contract for single jobs and array jobs.

#### Scenario: Single-job manifest survives the FastAPI route boundary
- **WHEN** the orchestrator submits a single job through `/api/v1/slurm/jobs`
- **THEN** `run_id`, `model_id`, `job_type`, and manifest fields MUST be available to RealSlurmGateway template rendering
- **AND** top-level request fields MUST override same-named nested manifest fields

#### Scenario: Array manifest survives the FastAPI route boundary
- **WHEN** the orchestrator submits an array job through `/api/v1/slurm/job-arrays`
- **THEN** `job_type`, `cycle_id`, `stage_name`, `tasks`, and nested `manifest` fields MUST be available to RealSlurmGateway template rendering
- **AND** top-level request fields MUST override same-named nested manifest fields

#### Scenario: Object store roots are rendered into real Slurm templates
- **WHEN** an array job is submitted with `object_store_root` and `object_store_prefix` in the orchestrator manifest
- **THEN** the rendered sbatch script MUST export those values to worker processes as `OBJECT_STORE_ROOT` and `OBJECT_STORE_PREFIX`
- **AND** the script MUST NOT silently fall back to `WORKSPACE_ROOT` for durable artifacts

### Requirement: Production jobs use fixed templates or constrained script mode
Real Slurm execution SHALL use fixed configured templates unless a constrained script mode is explicitly implemented and tested.

#### Scenario: Legacy single-job path submits to real Slurm
- **WHEN** a legacy or analysis orchestration path submits a single job to RealSlurmGateway
- **THEN** the job MUST resolve to an available configured template or a validated constrained script mode
- **AND** unsupported legacy `job_type` values MUST fail before submission with a clear validation error

#### Scenario: Template ownership is documented
- **WHEN** developers inspect Slurm template documentation
- **THEN** the docs MUST state which paths are canonical for real Slurm, which are legacy, and which orchestrator paths still use them

### Requirement: Retryable Slurm errors are stable
RealSlurmGateway SHALL map raw Slurm terminal states to stable control-plane error codes while preserving raw state details.

#### Scenario: Timeout becomes retryable
- **WHEN** `sacct` reports `TIMEOUT`
- **THEN** the pipeline job MUST persist `status=failed`, `error_code=SLURM_TIMEOUT`, and raw state metadata
- **AND** RetryService MUST treat the job as eligible for retry subject to retry limits

#### Scenario: Node failure becomes retryable
- **WHEN** `sacct` reports `NODE_FAIL` or `PREEMPTED`
- **THEN** the pipeline job MUST persist `error_code=NODE_FAILURE`
- **AND** RetryService MUST treat the job as eligible for retry subject to retry limits

#### Scenario: Out of memory preserves a stable error code
- **WHEN** `sacct` reports `OUT_OF_MEMORY`
- **THEN** the pipeline job MUST persist `error_code=OUT_OF_MEMORY`
- **AND** raw Slurm state metadata MUST be preserved for operator diagnosis

#### Scenario: Unknown terminal failure preserves raw state
- **WHEN** `sacct` reports a terminal failure state without a specific mapping
- **THEN** the pipeline job MUST persist a stable generic error code such as `SLURM_JOB_FAILED`
- **AND** raw Slurm state metadata MUST be preserved for operator diagnosis

#### Scenario: Poll timeout is persisted
- **WHEN** orchestrator polling exceeds `job_timeout_seconds`
- **THEN** the pipeline job, related run or cycle, and event log MUST be updated to a terminal failed state with `SLURM_JOB_TIMEOUT`
- **AND** retry scheduling MUST evaluate that failure through the same retry policy as Slurm-reported failures

### Requirement: Durable Slurm log lookup
Real Slurm log retrieval SHALL work after gateway restart and SHALL support array task logs.

#### Scenario: Master array job log request aggregates task logs
- **WHEN** logs are requested for an array master job id
- **THEN** the gateway MUST return available `%A_%a.out` and `%A_%a.err` content grouped or annotated by task id
- **AND** missing task logs MUST be reported without replacing existing task logs with an empty payload

#### Scenario: Gateway restart does not erase log metadata
- **WHEN** the gateway process restarts before logs are fetched
- **THEN** log lookup MUST use persisted job metadata or deterministic workspace patterns to locate logs
- **AND** the API response MUST indicate when metadata is incomplete

