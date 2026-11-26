# pipeline-control-state-consistency Specification

## Purpose
Keep the control plane's state transitions auditable: a manual repair marker and the scheduler's cancellation of an
active job each leave a pipeline event that says what happened to which job.

## Requirements

### Requirement: State transitions are auditable
A manual repair marker SHALL write an `ops.pipeline_event` record that includes actor context when available, trigger type, previous status, next status, retry count, and previous error details. The scheduler's cancellation of an active job (`cancel_active_cycle_jobs`) SHALL write a pipeline event of type `cancel` that records the Slurm job id and the reason. No API route writes either event (#3394).

#### Scenario: Retry event records prior failure
- **WHEN** a manual repair marker is recorded through `FileJournalRetryService.record_manual_repair`
- **THEN** a pipeline event MUST record trigger `manual`, previous job id, previous error code, and next retry count

#### Scenario: Cancel event records Slurm action
- **WHEN** the scheduler's `cancel_active_cycle_jobs` cancels an active job through the gateway
- **THEN** a pipeline event of type `cancel` MUST record the Slurm job id under `details.slurm.job_id` and the reason of the cancellation
