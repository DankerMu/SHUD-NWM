# Spec Delta: pipeline-job-persistence

## ADDED Requirements

### Requirement: Cohort runtime identity cross-check SHALL treat absent hydro_run identity fields as not-stored, not as mismatched

The file-journal runtime identity cross-check SHALL, for accepted-submit
forecast cohorts (`forecast_cohort_runtime_identity_matches`) and for each
cohort member, continue to require a per-model `hydro_run` row in the same
source and cycle that strictly matches on `run_id`, `model_id`,
`scenario_id`, `source_id`, `cycle_time`, and `submission_attempt`. For
`candidate_id`, `basin_id`, and `array_task_id` the check SHALL compare
strictly when the `hydro_run` row carries a value, and SHALL skip the field —
without failing the check — when the row's value is absent (`None`), because
the file-journal per-model writers do not persist these fields. A
present-but-different value SHALL remain fatal. The cohort-member side SHALL
remain fully strict, and the reconcile-side gates (exact master slurm id,
ownership user/account, stage-family job name, comment-when-stored, and the
array-task-id bijection against `cohort_members`) SHALL be unchanged.

#### Scenario: Production-shaped hydro_run rows reconcile to matched_bound

- **WHEN** an inflight forecast cohort's per-model `hydro_run` rows carry
  `None` for `candidate_id`, `basin_id`, and `array_task_id` (the shape
  written by the chain per-model trigger) and sacct returns a terminal master
  record passing all reconcile-side identity gates with a complete task
  bijection
- **THEN** restart reconcile SHALL record a `terminal` outcome with
  reconciliation decision `matched_bound` and project the per-task outcomes,
  instead of recording `identity_mismatch_blocked`

#### Scenario: Present-but-different identity fields still block

- **WHEN** a per-model `hydro_run` row carries a non-absent `candidate_id`,
  `basin_id`, or `array_task_id` that differs from the cohort member's value
- **THEN** the runtime identity cross-check SHALL fail and restart reconcile
  SHALL record `identity_mismatch_blocked` with zero durable writes

#### Scenario: Strict fields stay strict when degradable fields are absent

- **WHEN** a per-model `hydro_run` row has absent `candidate_id`,
  `basin_id`, and `array_task_id` but disagrees with the cohort member on
  `run_id`, `model_id`, `scenario_id`, `source_id`, `cycle_time`, or
  `submission_attempt` — or the row is missing entirely
- **THEN** the runtime identity cross-check SHALL fail and restart reconcile
  SHALL record `identity_mismatch_blocked` with zero durable writes
