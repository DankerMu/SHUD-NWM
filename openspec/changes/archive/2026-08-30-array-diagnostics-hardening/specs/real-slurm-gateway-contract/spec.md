## MODIFIED Requirements

### Requirement: Durable Slurm log lookup
Real Slurm log retrieval SHALL work after gateway restart, SHALL support array task logs, and SHALL never label one member's logs with another member's run or model identity. Production array scheduler logs SHALL be written to a cohort-neutral, submission-specific directory bound to the immutable manifest index; the directory SHALL exist before `sbatch` is invoked. Array log identity joins and restart discovery SHALL be bounded and SHALL fail safe: readable log content remains available when identity metadata is absent or invalid, but unproven identity MUST NOT be returned as complete.

#### Scenario: Master array job log request aggregates task logs with identity
- **WHEN** logs are requested for an array master job id whose exact manifest index is available
- **THEN** the gateway MUST return available `%A_%a.out` and `%A_%a.err` content grouped by task id and annotated with that entry's `model_id` and `run_id`
- **AND** missing task logs MUST be reported without replacing existing task logs with an empty payload

#### Scenario: Array templates use a cohort-neutral directory
- **WHEN** any production array template is rendered for members with different run ids
- **THEN** its output, error, and directory-creation paths MUST use one submission-specific neutral log directory and MUST NOT contain the first or any other member's run id
- **AND** submission MUST safely create that directory before invoking `sbatch`

#### Scenario: Direct array-template submitters preserve the rendered log binding
- **WHEN** a supported production acceptance or validation lane renders a production array template and invokes `sbatch` directly instead of `submit_job_array`
- **THEN** it MUST derive the same neutral directory from the workspace, cycle, and immutable manifest index through the canonical path contract and safely create it before invoking `sbatch`
- **AND** every post-submit task-log check and emitted evidence path MUST read that exact directory rather than a legacy member-run directory
- **AND** no-submit, blocked-preflight, and fake-validation lanes MUST NOT create the shared scheduler-log directory

#### Scenario: Gateway restart recovers exact array identity
- **WHEN** the gateway process restarts before logs are fetched
- **THEN** deterministic neutral-path discovery MUST locate the task logs and derive the one exact immutable manifest index for that submission without choosing a newest or otherwise guessed index
- **AND** the API response MUST indicate when record or identity metadata is incomplete

#### Scenario: Historical leader-run logs remain readable
- **WHEN** an array log exists only at the legacy `workspace/<leader-run-id>/logs/%A_%a` location
- **THEN** new log lookup MUST still return its available content
- **AND** it MUST annotate member identity only when the exact corresponding manifest index is proven

#### Scenario: Invalid identity metadata does not mislabel or erase logs
- **WHEN** the associated manifest index is missing, unsafe, oversized, malformed, ambiguous, or does not contain the requested task id
- **THEN** bounded lookup MUST return available log content with identity marked incomplete
- **AND** it MUST NOT guess a `model_id` or `run_id`, block on a non-regular file, or raise an unclassified exception
