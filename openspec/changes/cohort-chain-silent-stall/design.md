## Design

Change surface:
- `chain_stage_execution.poll_cycle_stage_until_terminal` and its callers `submit_and_wait_cycle_stage` / `resume_cycle_stage`.
- The `chain_forecast_execution` stage-loop span counters.
- File-journal `candidate_state` (classification before compaction) and `_CYCLE_SCOPE_JOB_PROJECTION_KEYS`.
- `chain_repository_state.candidate_state_from_rows`.
- `chain_source_cycle._job_belongs_to_candidate`.
- `scheduler_state_rows._shared_stage_cycle_run_matches_candidate`, plus the identity filter and attempt floor consumers.
- The downstream-stage cohort master-row writer (B4).
- The `chain_array_accounting` forecast cohort projection.

Must preserve:
- The existing governed paths:
  - the submit try/except yielding `submit_result_ambiguous` / `_record_submission_failure`;
  - the poll timeout path `record_cycle_stage_poll_timeout` when zero gateway query failures occurred;
  - the accepted-submit runtime CAS: `ACCEPTED_SUBMIT_RUNTIME_TRANSITION_CONFLICT` stays a governed conflict;
  - the `_schedule_cycle_stage_retry` status set.
- Single-model cohort decisions, including #2584's ambiguous auto-retry.
- `has_active_pipeline` semantics (#2543).
- The master-row reservation `restart_stage="forecast"`.
- The reconcile leg (`reconcile.py` member restart_stage).
- Every currently credited success.
- Decisions on historical rows without any recorded `cohort_members`.
- The DB repository query: its SELECT (`chain_repository_state` ~505-533) carries no `cohort_members` and never selects `_cohort_<d>` rows, so B is a no-op there. The query is not widened, and bare `cycle_<src>_<stamp>` rows keep their cycle-wide semantics.
- A Slurm job whose id is already bound is never submitted a second time because a gateway query or persistence write failed.

Root-cause status of #2570 A: the only artifacts that could have named the raise point are gone. These were the 09-21…09-23 `resource_limit_blocked` pass evidence, whose `model_run_failures` tails had been preserved since #2572, and evidence retention purged them. A read-only scan of every remaining node-22 pass artifact on 2026-09-26 found zero `error_traceback_tail`. A is therefore hardening of the three structurally identified poll-loop sites and their post-poll siblings, not a confirmed-cause fix. The PR says so.

Must add / change:

- **A1 gateway query.**
  - `get_job_status` exceptions inside `poll_cycle_stage_until_terminal` are caught. The loop keeps polling, counting failures and keeping the last error type.
  - If the deadline is reached after one or more query failures, the poll ends on a non-resubmitting lane for **every** stage. It returns a governed observation mapped to stage result `reconcile_unverified` with `error_code="SLURM_STATUS_QUERY_UNAVAILABLE"` and the failure count.
  - No terminal status is written, and the row keeps its bound Slurm id for next-pass restart reconcile.
  - Reason: today a non-cohort timeout writes `failed/SLURM_JOB_TIMEOUT`, which is transient in `retry.py`. That would resubmit a job whose status is merely unknown.
  - A deadline with zero query failures keeps today's timeout path unchanged.

- **A2 status write.**
  - `_update_runtime_pipeline_status` raising anything except the governed accepted-submit conflict makes the poll return a `TerminalJobObservation` with a persist-failure marker (like the `poll_timed_out` marker) and `error_code="STAGE_RUNTIME_STATUS_PERSIST_FAILED"`.
  - On that marker, both callers skip every post-poll repository write and gateway call: aggregate, status override, accounting event, log publish, `get_pipeline_job`, and `_after_cycle_stage_terminal`.
  - Both callers return stage result `reconcile_unverified`. `chain_forecast_execution` maps that to `reconciling`, which is not in the retry set, so there is no `_schedule_cycle_stage_retry`.
  - `scheduler_execution` therefore records the unit's members as reconciling, not `submission_failed` / `PRODUCTION_ORCHESTRATION_FAILED`.
  - The row stays in its pre-failure runtime status with its bound Slurm id. On the next pass, `_job_needs_submission` is false, so `resume_cycle_stage` polls it; `_job_needs_restart_reconcile` also accepts it.

- **A3 event write.** When `insert_pipeline_event` raises after a successful status write, a warning is logged and a counter is carried on the observation and its evidence. The loop continues.

- **A4 span counters.** The stage span gets the real `basin_count` (`basin_count_at_entry`) on every exit path by being set at stage entry. An exception that still propagates is re-raised unchanged.

- **B1 three-valued membership.** This reuses the existing #2543 rule `_complete_cohort_members_by_run`, the one rule behind `has_active_pipeline`; there is no digest recompute.
  - Where: in file-journal `candidate_state`, **before** `_compact_cycle_scope_job` (which strips `cohort_members`). Each model-less `cycle_<src>_<stamp>_*` row is classified from the cycle's rows.
  - Row-level first: a row that records its own `cohort_members` (forecast rows, B4 rows) is judged against its own list, with the same completeness checks. Only rows without their own list use the run-level union. This is why partial forcing is correct: a dropped member is `non_member` for the narrowed forecast/state_save_qc rows. `has_active_pipeline` keeps its run-level rule unchanged (must-preserve).
  - `member`: the applicable complete member list (the row's own, else the run's) contains the candidate's `model_id`.
  - `non_member`: the run has complete recorded members without the candidate. The row is excluded exactly as `has_active_pipeline` excludes it.
  - `incomplete`: the run records a truncated, mismatched, cap-sized, or blank-model member list.
  - `unwitnessed`: no row of the run records members. This covers historical rows and the bare cycle run id.
  - Kept rows carry the class in a new projection key (e.g. `cohort_membership`) added to `_CYCLE_SCOPE_JOB_PROJECTION_KEYS`. Downstream consumers read the annotation and never recompute it.

- **B2 failure side.** `member` rows count as the candidate's own in all of these:
  - `chain_repository_state` `latest_failed_job`, `retry_count`, and permanence;
  - `chain_source_cycle._job_belongs_to_candidate` (or the equivalent row filter);
  - `scheduler_state_rows._shared_stage_cycle_run_matches_candidate` decision authority;
  - the `scheduler_state_identity_filter` row set and attempt floor.

  A permanently failed member-cohort `state_save_qc` results in `blocked/permanent_failure_guard`, `failure.stage=state_save_qc`, `automatic_retry_allowed=false`. For a transient failure, attempt = the cohort row's `retry_count`, and automatic retry stops at the budget.

- **B2b incomplete and permanently failed.** When a `permanently_failed` model-less row is classified `incomplete`, the candidates it is visible to are `blocked` with typed reason `cohort_membership_unprovable`, not auto-retried. This is the fail-closed direction #2603 requires.
  - Precedence: evaluated at the same point as `permanent_failure_guard` in `scheduler_state_decision`.
  - A candidate with its own terminal success for the cycle (a model-bound row, or a `member` row with terminal completion) is not blocked by it. Terminal skip wins.
  - Operator exit: a manual-retry marker clears it, exactly as it clears `permanent_failure_guard`.
  - Tests cover all three.

- **B3 success side.** `member` and `unwitnessed` rows keep crediting completed-stage and terminal success as today. `non_member` rows are excluded by B1. Every currently credited success is preserved.
  - Accepted deviation: `unwitnessed` rows keep the old asymmetry (success credited, failure not attributed).
  - B4 closes this for all new rows.

- **B4 write side.**
  - Every model-less cohort master row of a stage downstream of forecast (`state_save_qc`, and `parse`/`publish` if they run as model-less cohort rows) records `cohort_members` at row creation. The members are the basins' `{model_id, candidate_id, run_id}` in the existing member shape (compatible with `ordered_cohort_members`).
  - New `…_state_save_qc_cohort_<d'>` restart cohorts, including strict subsets of the original members, are thereby witnessed by their own rows.
  - The convert row is #2546 (batch S2) and out of scope here.
  - Shape: only `cohort_members` is added. The row must not carry accepted-submit master markers (`accepted_submit_contract_version`, `cohort_digest`, `expected_slurm_*`, `submit_outcome`), so `normalize_accepted_submit_evidence`, `_reconcile_inventory_row_kind` and the `upsert_pipeline_job` master freeze treat it exactly as before.
  - Gated on `supports_accepted_submit_reconcile`, like the forecast/forcing branches, so the DB `reserve_pipeline_job` path is unchanged.
  - Test: B4 `state_save_qc` rows and their `_retry_N` rows behave identically to origin/master under journal validation, restart-reconcile inventory and upsert merge.
  - Compaction strips members from candidate states. Note the per-row journal size.

- **C1** A failed or unverified task projection gets `restart_stage` = `"forecast"`.

Governing invariant: a model-less cycle-scope cohort row whose run has complete recorded membership affects exactly its members, on both the success and failure sides. Unwitnessed rows keep their pre-change semantics, and incomplete rows fail closed. No transient gateway or persistence exception inside a stage poll can end a unit's chain without a governed, typed, non-resubmitting result.

Sibling surfaces:
- `chain_forecast_execution._poll_until_terminal` (the per-candidate chain poll, ~1658) has the same unguarded shape. Audit it and apply A1–A3, or record why not.
- The post-poll writes in `submit_and_wait_cycle_stage` / `resume_cycle_stage` are covered by the A2 short-circuit only on the marker. Any other unguarded post-poll write stays as-is and is listed in the PR.
- `chain_analysis.py` stage success writes: there is no poll loop; state this.
- `reconcile.py:1981` member restart_stage is already `"forecast"`.
- `has_active_pipeline` / `_cohort_run_ids_excluding_model` is the membership rule owner and is reused, not duplicated.
- The DB path (no-op, above).
- Out of scope, recorded:
  - #2655, the reserved-unbound forecast fallback reconcile ambiguity behind the live node-22 stall.
  - Its "downstream state_save_qc launched while the forecast master was still reserved" symptom. If B changes that, say so in the PR; otherwise it stays with #2655.

Seams under test:
- `poll_cycle_stage_until_terminal` via real `submit_and_wait_cycle_stage` / `resume_cycle_stage` / `orchestrate_cycle`, with gateway and repository fakes.
- A real `orchestrate_cycle` + `FileOrchestrationJournalRepository` + scheduler `_candidate_state_decision` loop, in the style of `tests/test_state_save_submit_ambiguity.py::_run_cycle`, extended to multi-member cohorts. Run ids come from the real `candidate_execution_cohort_run_id`; digests are never hand-written.
- `record_cycle_stage_status_override` with FileJournal for #2559.

Required evidence (input → expected):
- **A1**
  - The gateway `get_job_status` raises twice, then returns terminal → the stage succeeds, no exception escapes, and the unit's downstream stages run.
  - A convert (non-cohort) stage whose gateway query raises until the deadline → `reconcile_unverified` / `SLURM_STATUS_QUERY_UNAVAILABLE`. The gateway submit count is unchanged, the row keeps its Slurm id, and no terminal status is written.
  - A deadline with zero query failures → today's timeout behavior.
- **A2.** The fake repository raises `OSError` on every write of that stage after the first failure.
  - Pass 1: stage result `reconcile_unverified` / `STAGE_RUNTIME_STATUS_PERSIST_FAILED`. There are no post-poll writes and the submit count is unchanged. The span `basin_count` equals the entry count. The unit's members are reconciling, not `submission_failed`. Other units in the pass are unaffected.
  - Pass 2, with the fake healed: the row reaches its real terminal status, downstream stages continue, and the submit count across both passes is unchanged.
- **A3.** `insert_pipeline_event` raises once → the chain continues to terminal and the next stages, and the failure is counted.
- **A4.** An injected unexpected exception still propagates, and the span `basin_count` is real.
- **B**
  - A 3-member cohort's `state_save_qc`, in the same run as its forecast rows with members, exhausts its retries → `permanently_failed` → each member is `blocked/permanent_failure_guard` with `automatic_retry_allowed=false`.
  - A transient failure across two or more passes → attempt increments per pass, equals the cohort `retry_count`, and stops at the budget.
  - A `…_state_save_qc_cohort_<d'>` restart cohort that is a strict subset of the original members (its rows carry members via B4): a permanent failure blocks its members; a success is credited and not resubmitted.
  - A sibling non-member cohort's failure or success → the non-member's decision is unchanged.
  - Forcing partially failed, so the forecast members are a subset → surviving members' rows are attributed and dropped members' are not.
  - An incomplete member record plus a permanent failure → `blocked/cohort_membership_unprovable`.
  - An unwitnessed historical row (no members anywhere in the run) → decisions identical to origin/master.
  - Single-model cohort decisions (the existing `tests/test_state_save_submit_ambiguity.py` cases, #2584) are unchanged.
  - The DB-path decision tests are unchanged.
- **C.** Basins with `restart_stage` `convert` and `forcing`, one failed and one missing task → the projection is persisted with `restart_stage="forecast"` and the master is not deferred as `identity_mismatch_blocked`. Red before the fix shows the deferral. All-success cohorts are unchanged.

Non-goals:
- #2570 groups B/C (shipped).
- The #2655 fallback bind.
- Changing `ACCEPTED_RESTART_STAGES`.
- Convert-row members (#2546, S2).
- Widening the DB query.
- #2542/#2555 (S2).

Review focus:
1. A1/A2 never resubmit a bound job, and the next pass resolves it.
2. B1 classification happens before compaction and is read, never recomputed, downstream.
3. B3/B4 preserve every credited success, and new restart cohorts are witnessed.
4. Single-model, unwitnessed, and DB lanes are unchanged.
5. The sibling `_poll_until_terminal`.
6. #2559 success projection and the reconcile leg are unchanged.
