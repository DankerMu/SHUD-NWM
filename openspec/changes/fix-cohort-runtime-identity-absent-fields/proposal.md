# Fix forecast-cohort runtime identity check rejecting absent hydro_run fields

## Why

`services/orchestrator/file_orchestration_journal.py:1297`
(`forecast_cohort_runtime_identity_matches`) cross-checks each accepted-submit
cohort member against the per-model `hydro_run` journal row with a strict
six-field comparison: `candidate_id`, `run_id`, `model_id`, `basin_id`,
`scenario_id`, `array_task_id`.

But in the file-journal deployment the per-model `hydro_run` rows are written
by the chain per-model trigger
(`services/orchestrator/chain_forecast_trigger.py:169` →
`create_hydro_run(context, manifest)`), and that writer never populates
`candidate_id`, `basin_id`, or `array_task_id` — the manifest and run context
carry none of them, so all three persist as `None`
(`file_orchestration_journal.py:1203-1208`). The validator then compares
`""`/`int(None)` against the cohort member's real values, so it returns
`False` for **every** inflight forecast cohort, and
`services/orchestrator/reconcile.py:1022` records
`identity_mismatch_blocked` on every restart-reconcile pass. A
`reconcile_unverified` forecast cohort can therefore never be flipped back to
`succeeded` by reconcile, no matter what sacct proves.

Field evidence (2026-08-03, node-22): IFS 2026071100 cohort
`job_cycle_ifs_2026071100_forecast_cohort_0e20678419f7_forecast` — sacct
showed `24254_0..5` all COMPLETED and all six `hydro_run` rows `succeeded`,
yet restart-reconcile emitted `identity_mismatch_blocked` (pass evidence
`/scratch/frd_muziyao/nhms-prod/workspace/scheduler/evidence/scheduler_2026080301_16d7474da0c3.json`).
Recovery required a manual
`project_forecast_cohort_tasks(..., reconciliation_decision="matched_bound")`
bypass (archived at
`/ghdc/data/nwm/recovery/ifs-071100-cohort-projection-20260803T0230Z/`).

Existing tests never caught this because the fixture
(`tests/test_gateway_reconcile.py::_append_cohort_placeholders`) seeds
`hydro_run` rows **with** all three fields — a shape production never writes.

## What Changes

- `forecast_cohort_runtime_identity_matches` degrades per field when the
  `hydro_run` row's value is absent (`None`): an absent `candidate_id`,
  `basin_id`, or `array_task_id` skips that field's comparison instead of
  failing it. A present-but-different value stays fatal. The remaining gates
  are untouched and still strict: `run_id`, `model_id`, `scenario_id`,
  `source_id`, `cycle_time`, `submission_attempt` inside the validator, plus
  the reconcile-side gates (exact master slurm id, ownership user/account,
  stage-family job name, comment when stored, and the exact
  array-task-id bijection against `cohort_members` in
  `_terminal_file_cohort_identity_matches`).
- Regression tests in `tests/test_gateway_reconcile.py`: a cohort whose
  `hydro_run` rows carry `None` for all three fields (production shape)
  reconciles to `terminal`/`matched_bound`; a row with a
  present-but-different value for any of the three fields stays
  `identity_mismatch_blocked` with zero durable writes.

## Why validator degrade instead of writing the fields

Writing `candidate_id`/`basin_id`/`array_task_id` into new `hydro_run` rows
would not unwedge any cohort already in the journal (the wedged rows are
immutable history — the same reason `append_historical_hydro_run` exists),
would require a coordinated writer + reconcile deploy, and the three fields
are redundant as identity evidence: `run_id` is the per-model primary key the
member also carries, and the task-id bijection is already enforced against
sacct in `_terminal_file_cohort_identity_matches`. Absent-is-skip,
present-but-different-is-fatal matches the established precedent for the
sacct comment gate (`reconcile.py:1131-1139`).

## Out of Scope

- No change to `hydro_run` writers or the journal record schema.
- No change to the accepted-submit cohort-member schema or
  `forecast_cohort_identity_is_valid` (members stay fully strict).
- No change to reconcile outcome vocabulary or the
  `identity_mismatch_blocked` → `identity_mismatch_released` convergence
  machinery.
- No retroactive re-projection of the manually recovered IFS 2026071100
  cohort — already archived.
