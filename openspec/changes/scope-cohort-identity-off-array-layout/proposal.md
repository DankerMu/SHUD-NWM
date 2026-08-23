# Proposal: scope cohort runtime identity off the array layout

## Why

`forecast_cohort_runtime_identity_matches` compares `hydro_run.array_task_id`
against the cohort member's `array_task_id` and fails the **whole cohort** on
any single mismatch. But `array_task_id` is the index a member happened to
occupy in **the last array submission that wrote its row** — a per-submission
layout artefact, not a cross-submission identity.

When a cohort's member set changes, task numbers are renumbered. Every member
whose `hydro_run` row was written by an earlier, differently-sized submission
now carries a stale index, and the gate returns False for the cohort.

Measured on node-22 (`gfs_2026080712`, issue #1749 triage comment): the cycle
was submitted four times with **17 / 15(+retry) / 7 / 22** members. On the
22-member cohort, **20 of 22** members' recorded task numbers disagreed with the
new layout. The two that agreed did so by coincidence — those basins occupied
positions 0 and 1 in both layouts.

Consequence, and why this is `priority:high`: identity False ->
`identity_mismatch_released` -> `reservation_lost` -> the idempotency key is
wedged -> the forecast stage submits zero every pass. This is the first link in
the #1748 P0 production stall.

## What changes

Remove `array_task_id` from the identity projection of
`forecast_cohort_runtime_identity_matches`. Nothing else in the gate moves:
`run_id`, `model_id`, `scenario_id`, `source_id`, `cycle_time`, and
`submission_attempt` stay strict, and the `candidate_id` / `basin_id` legs keep
their present-but-different-is-fatal semantics.

The stale premise that produced the defect is corrected in two places, both
recorded in place rather than rewritten away: the comment above the comparison,
and the donor change's design (`fix-cohort-runtime-identity-absent-fields`).
The donor is corrected on this branch but **not** archived here — see design D8
for why archiving it in this PR would land a clause that this PR's own code
falsifies. Both changes archive together in the post-merge chore commit, donor
first.

## What does not change

- Reconcile-side gates. In particular `_terminal_file_cohort_identity_matches`
  builds its bijection from **live sacct** `array_task_records` against the
  **current** master's `cohort_members` — a different input from `hydro_run`,
  so this change does not weaken it. See design D3.
- `project_forecast_cohort_tasks`'s use of `array_task_id` on pipeline-job rows.
  That is layout-scoped within one submission's outcome mapping, which is a
  legitimate use of a layout index. See design D5.
- Already-wedged rows. A cohort sitting in `identity_mismatch_released` is in a
  non-reclaimable terminal; fixing the gate does not revive it. That exit is
  #1748's scope, and the acceptance criterion here is written accordingly.

## Non-goals

- #1748's remaining links (no exit from the permanent terminal, the db-free
  silent decline, the `resource_limit_blocked` mask).
- #1736's whole-cohort reselection, which is the upstream source of the layout
  churn. This change makes the gate immune to churn whether or not #1736 lands.
- `submission_attempt`, which is the same defect one field over — filed as
  **#1792**, deliberately not fixed here. See design D4.
