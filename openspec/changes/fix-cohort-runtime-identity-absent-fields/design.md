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
the run context has no `basin_id` attribute at that point, so they persist
`None`. The strict comparison (`str(None or "")` vs a real member value,
`int(None)` → `TypeError` → swallowed → `False`) then fails for such rows.

> **Correction (2026-08-23, issue #1749).** The sentence above is true of the
> `create_hydro_run` path and **false as a statement about production**, which
> is how it was used to justify the degradation. Array-shaped production
> cohorts do not go through that writer: they go through
> `create_hydro_run_from_basin`, called from `chain_manifests.py:386`, and that
> writer persists **all three** fields. Measured on node-22 (`gfs_2026080712`,
> 48 `hydro_run` records): every sampled row carries non-null `candidate_id`,
> `basin_id`, and `array_task_id`. The degradation's `None` branch is therefore
> dead on production data, and the present-but-different leg — which this
> change deliberately kept fatal — is the leg that actually runs. For
> `array_task_id` that leg is a defect, because the value is a per-submission
> layout index rather than an identity; issue #1749 removes it from the
> identity projection. The line references `1203/1207/1208` above have also
> drifted and today land on unrelated code. This correction is recorded in
> place rather than by rewriting the paragraph, so the reasoning that produced
> the degradation stays auditable.



## Goals / Non-Goals

- Goal: reconcile must be able to prove identity for cohorts whose
  `hydro_run` rows follow the production shape, without weakening any gate
  that operates on data production actually writes.
- Non-goal: making the writers populate the three fields (does not fix
  wedged history, couples two planes in one deploy).
- Non-goal: any relaxation on the cohort-member side or on check 1.

> **Line-reference drift disclosure (2026-08-23, round 4 of issue #1749).** Every
> `file:line` reference in this change's `proposal.md` and `design.md` was
> written against the tree as it stood on 2026-08-03 and has since drifted.
> Mechanically checked at head (`9e962bd3`); the ones that no longer land on
> what they name:
>
> | cited | names | actual at head |
> |---|---|---|
> | `file_orchestration_journal.py:1297` | `forecast_cohort_runtime_identity_matches` | `:1784` |
> | `file_orchestration_journal.py:1203-1208` | the `None`-persisting writer path | `create_hydro_run` at `:1685-1716` |
> | `reconcile.py:1124` | `_terminal_file_cohort_identity_matches` | `:1178` |
> | `reconcile.py:1022` | the `identity_mismatch_blocked` record | `:1081` |
> | `reconcile.py:1131-1139` | the sacct comment "not stored ≠ different" precedent | not re-derived; locate by symbol |
>
> These are **not** renumbered in place. This change is a completed piece of
> history and its citations are a record of the tree it was written against;
> renumbering them would make the document silently disagree with its own
> commits. Locate by symbol name, not by line. Issue #1749's own documents are
> held to head-accurate citations because they are still live.

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

> **Superseded at head (2026-08-23, issue #1749).** Both bullets above count
> three degradable fields, and "the regression tests pin it in both
> directions" is no longer true of `array_task_id`: the
> `scope-cohort-identity-off-array-layout` change deleted that comparison, so
> nothing pins a present-but-different `array_task_id` as fatal — by design,
> because a renumbered layout index makes that state normal. The claim still
> holds for `candidate_id` and `basin_id`.


## Migration

None. Behavior change is read-side only; wedged cohorts self-heal on the
next restart-reconcile pass once sacct identity proves out.
