## MODIFIED Requirements

### Requirement: Retry Identity — Same run_id, New slurm_job_id

Retry jobs SHALL maintain continuity with the original run while obtaining fresh Slurm resources.

#### Scenario: Retry preserves run_id

- **WHEN** a retry job is created (either manual or automatic)
- **THEN** the new `pipeline_job` SHALL use the same `run_id` as the original failed job
- **THEN** the new `pipeline_job` SHALL use the same `cycle_id`, `source`, `stage`, and `model_id` as the original

#### Scenario: Retry gets new slurm_job_id

- **WHEN** a retry job is submitted to Slurm
- **THEN** a new `slurm_job_id` SHALL be assigned by Slurm and stored in the new `pipeline_job` record
- **THEN** the original failed job's `slurm_job_id` SHALL remain unchanged for audit purposes

#### Scenario: Retry gets new job_id

- **WHEN** a retry job is created
- **THEN** a new `job_id` SHALL be generated for the retry job as the deterministic string `<base>_retry_<n>`, where
  `n` is the retry attempt and is at least 1
- **THEN** when a `job_id` carries more than one `_retry_<n>` suffix, the last suffix SHALL be the authoritative attempt
- **THEN** the relationship between original and retry jobs SHALL be traceable via `pipeline_event.details_json.previous_job_id`
