# Spec Delta: pipeline-job-persistence

## MODIFIED Requirements

### Requirement: Cohort runtime identity cross-check SHALL treat absent hydro_run identity fields as not-stored, not as mismatched

The file-journal runtime identity cross-check SHALL, for accepted-submit
forecast cohorts (`forecast_cohort_runtime_identity_matches`) and for each
cohort member, continue to require a per-model `hydro_run` row in the same
source and cycle that strictly matches on `run_id`, `model_id`,
`scenario_id`, `source_id`, `cycle_time`, and `submission_attempt`. For
`candidate_id` and `basin_id` the check SHALL compare strictly when the
`hydro_run` row carries a value, and SHALL skip the field — without failing
the check — when the row's value is absent (`None`), because some
file-journal per-model writer paths do not persist these fields; a
present-but-different value SHALL remain fatal for these two fields.

The check SHALL NOT compare `array_task_id` at all. An array task id is the
index a member occupied within a single array submission and is frozen on the
`hydro_run` row at the submission that created it, so it is not stable across
submissions: whenever a cohort's member set changes the indices are renumbered
and previously written rows carry stale values. Comparing it made the gate
fail the entire cohort on layout churn alone.

The cohort-member side SHALL remain fully strict, and the reconcile-side gates
(exact master slurm id, ownership user/account, stage-family job name,
comment-when-stored, and the array-task-id bijection against `cohort_members`)
SHALL be unchanged — that bijection draws its task numbers from live sacct
records rather than from `hydro_run`, so it is unaffected.

#### Scenario: A renumbered member set no longer fails the cohort

- **WHEN** a cohort is submitted whose member set differs from an earlier
  submission for the same source and cycle, so that members' array task
  indices are renumbered, and each member's per-model `hydro_run` row still
  carries the array task id written by that earlier submission, and every
  other identity field agrees
- **THEN** the runtime identity cross-check SHALL pass, and restart reconcile
  SHALL NOT record `identity_mismatch_blocked` or `identity_mismatch_released`
  on the basis of the array task id

#### Scenario: Present-but-different sibling fields still block

- **WHEN** a per-model `hydro_run` row carries a non-absent `candidate_id` or
  `basin_id` that differs from the cohort member's value
- **THEN** the runtime identity cross-check SHALL fail and restart reconcile
  SHALL record `identity_mismatch_blocked` with zero durable writes

#### Scenario: Strict fields stay strict when degradable fields are absent

- **WHEN** a per-model `hydro_run` row has absent `candidate_id` and
  `basin_id` but disagrees with the cohort member on `run_id`, `model_id`,
  `scenario_id`, `source_id`, `cycle_time`, or `submission_attempt` — or the
  row is missing entirely
- **THEN** the runtime identity cross-check SHALL fail and restart reconcile
  SHALL record `identity_mismatch_blocked` with zero durable writes

#### Scenario: Production-shaped hydro_run rows reconcile to matched_bound

- **WHEN** an inflight forecast cohort's per-model `hydro_run` rows carry
  `None` for `candidate_id` and `basin_id` (the shape written by
  `create_hydro_run`) and sacct returns a terminal master record passing all
  reconcile-side identity gates with a complete task bijection
- **THEN** restart reconcile SHALL record a `terminal` outcome with
  reconciliation decision `matched_bound` and project the per-task outcomes,
  instead of recording `identity_mismatch_blocked`
