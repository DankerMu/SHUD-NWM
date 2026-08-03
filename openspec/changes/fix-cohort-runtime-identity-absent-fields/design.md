# Design: fix-cohort-runtime-identity-absent-fields

## Context

Restart reconcile for a file-journal forecast cohort layers two identity
checks before it will project sacct results into durable rows:

1. `_terminal_file_cohort_identity_matches` (`services/orchestrator/reconcile.py:1124`)
   — master slurm id, ownership, job-name stage family, comment-when-stored,
   and the array-task-id bijection between sacct task records and
   `cohort_members`.
2. `forecast_cohort_runtime_identity_matches`
   (`services/orchestrator/file_orchestration_journal.py:1297`) — for each
   cohort member, an independently written per-model `hydro_run` row must
   exist in the same source/cycle and agree on identity fields.

Check 2 was written against the test-fixture row shape
(`_append_cohort_placeholders`), which carries `candidate_id`, `basin_id`,
and `array_task_id`. The production writer path
(`chain_forecast_trigger.trigger_forecast_impl` → `create_hydro_run`) never
sets those three: the run manifest has no `candidate_id`/`array_task_id`, and
the run context has no `basin_id` attribute at that point, so
`file_orchestration_journal.py:1203/1207/1208` persist `None`. The strict
comparison (`str(None or "")` vs a real member value, `int(None)` →
`TypeError` → swallowed → `False`) then fails for every real cohort.

## Goals / Non-Goals

- Goal: reconcile must be able to prove identity for cohorts whose
  `hydro_run` rows follow the production shape, without weakening any gate
  that operates on data production actually writes.
- Non-goal: making the writers populate the three fields (does not fix
  wedged history, couples two planes in one deploy).
- Non-goal: any relaxation on the cohort-member side or on check 1.

## Decision

Per-field absent-is-skip, present-but-different-is-fatal, applied only to the
`hydro_run` side of the comparison and only for `candidate_id`, `basin_id`,
`array_task_id`:

```
strict:      run_id, model_id, scenario_id            (string compare, as today)
degradable:  candidate_id, basin_id                   (skip iff row value is None)
degradable:  array_task_id                            (skip iff row value is None,
                                                       else int compare, as today)
strict:      source_id, cycle_time, submission_attempt (row-level, as today)
```

`None` means "the writer never had this datum" (JSON `null` / absent key).
An empty string or any other present value still compares strictly — a row
that *claims* an identity must match it. This mirrors the sacct comment
precedent at `reconcile.py:1131-1139` ("not stored" ≠ "different").

Why this stays safe: the degraded fields are corroborating evidence, not the
primary binding. The cohort master row is queried by its own durably bound
slurm id; `run_id` (the per-model primary key), `model_id`, `scenario_id`,
source, cycle, and attempt still must agree row-by-row; and the task-id
bijection against sacct is enforced independently in check 1. An attacker row
would have to collide on all of those inside the same locked cycle to slip
through — exactly the pre-change trust base for rows produced by the real
writer, which never carried the three fields anyway.

## Risks / Trade-offs

- Risk: a future writer that populates the three fields with wrong values
  would previously have been (accidentally) blocked; now only
  present-but-different blocks. That is the intended semantics — and the
  regression tests pin it in both directions.
- Trade-off: identity strength for fixture-shaped rows is nominally lower
  (three fewer mandatory comparisons when absent). Accepted: production rows
  never had that strength; the fixture strength was fictional.

## Migration

None. Behavior change is read-side only; wedged cohorts self-heal on the
next restart-reconcile pass once sacct identity proves out.
