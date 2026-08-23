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

> **Correction (2026-08-23, issue #1749).** Two premises above were false when
> written. They are corrected here rather than deleted, so the record of what
> this change believed stays readable:
>
> - "returns `False` for **every** inflight forecast cohort" — false.
>   `create_hydro_run_from_basin`
>   (`services/orchestrator/file_orchestration_journal.py:1527`, called from
>   `chain_manifests.py:386`) does persist all three fields, so cohorts written
>   through that path never reach the absent-field path at all. The defect was
>   real but scoped to the `create_hydro_run` writer
>   (`file_orchestration_journal.py:1685-1716`), not universal.
> - "a shape production never writes" — false for the same reason:
>   `create_hydro_run_from_basin` writes exactly that all-three-fields shape in
>   production, so the fixture was not fictional, only unrepresentative of the
>   *other* writer.
>
> Neither correction changes what this change shipped; both are premises its
> reasoning did not actually need.


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

> **Superseded at head (2026-08-23, issue #1749).** The two bullets above were
> accurate for what this change shipped; their `array_task_id` half is no
> longer head behaviour. `array_task_id` is a per-submission layout index, not
> an identity: any member-set change renumbers every index while the frozen
> `hydro_run` row keeps the old one, so present-but-different is the *normal*
> post-renumber state rather than evidence of a mismatch. The
> `scope-cohort-identity-off-array-layout` change removes that comparison
> outright; `candidate_id` and `basin_id` keep the absent-is-skip /
> present-but-different-is-fatal rule described above, and the regression
> parametrization covers those two fields only.


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
