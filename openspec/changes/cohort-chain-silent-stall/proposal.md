## Why

Three defects let a DB-free scheduler cohort chain stall silently or pick the wrong retry lane:

- **#2570, group A only.** The issue's corrected attribution is in its comments; groups B and C were already shipped by #2572 and #2579. The chain stage poll loop (`chain_stage_execution.poll_cycle_stage_until_terminal`) calls the Slurm gateway (`get_job_status`) and two repository writes (`_update_runtime_pipeline_status` and `insert_pipeline_event`) with no exception isolation. One transient failure escapes the stage loop and the unit's whole `orchestrate_cycle`. `scheduler_execution` then records `submission_failed` for every member, the unit's remaining stages never run in that pass, and the stage span is committed with `basin_count=0/submitted_count=0`. node-22 hit this five times (2026-09-21…09-23), once stranding 47 IFS models.
- **#2603.** When a multi-member (model-less) cohort's `state_save_qc` fails permanently, the failure is invisible to the member candidates. The success side attributes the cycle-scope cohort row to members, but the failure side and the decision authority drop model-less rows. Members therefore auto-retry with `attempt 0` forever and bypass `permanent_failure_guard`. A single-model cohort takes a different lane for the same failure.
- **#2559.** The forecast cohort projection for failed or unverified array tasks copies the basin's top-level `restart_stage` (`convert`/`forcing`). `normalize_candidate_projections` rejects that value, so an ordinary forecast failure is downgraded to `identity_mismatch_blocked` / `SLURM_TASK_IDENTITY_MISMATCH` and per-task accounting is lost.

## What Changes

- **#2570 A (hardening).** The three poll-loop call sites become governed.
  - A gateway status query failure is retried in-loop. If the deadline arrives after one or more failures, the stage ends on the non-resubmitting `reconcile_unverified` / `SLURM_STATUS_QUERY_UNAVAILABLE` for every stage.
  - A non-conflict status write failure sets a persist-failure marker. Both callers then skip every post-poll write and gateway call, and end the stage `reconcile_unverified` / `STAGE_RUNTIME_STATUS_PERSIST_FAILED`. Nothing is resubmitted, and the next pass resolves the still-bound job.
  - An event write failure is counted and does not stop the chain.
  - The stage span records the real `basin_count` on every exit path.
- **#2603.** Model-less cohort rows are classified before compaction into member / non_member / incomplete / unwitnessed.
  - A row that records its own `cohort_members` is judged by its own list. Other rows use the #2543 run-level rule (`_complete_cohort_members_by_run`).
  - Member rows count on both the failure and success sides. Non-member rows are dropped.
  - A permanently failed incomplete row blocks with `cohort_membership_unprovable`.
  - Unwitnessed (historical) rows keep today's semantics.
  - **B4 (write side).** `chain_forecast_orchestrator_cycle._reserve_cycle_stage` records `cohort_members` on model-less cohort master rows of stages downstream of forecast. The row gets members only, with no accepted-submit markers, and this applies only on the `supports_accepted_submit_reconcile` repository.
- **#2559:** failed and unverified forecast cohort task projections always carry `restart_stage="forecast"`.

## Triage

```text
Issue type: bugfix
Fixture level: expanded
Upstream suggested level: absent
Blast radius: every DB-free cohort chain on node-22 (IFS+GFS, up to 47 members): silent stalls, unbounded state_save_qc resubmission, misclassified forecast failures
Selected risk packs: Concurrency/shared state/ordering; Error handling/rollback/partial outputs; Legacy compatibility (historical journal rows); Schema/field names (projection enum); Documentation
Evidence floor: new regression tests through real orchestrate_cycle + FileOrchestrationJournalRepository + _candidate_state_decision; existing chain/orchestration/state-decision suites; node-27 pytest oracle
```

## Impact

- `services/orchestrator/chain_stage_execution.py`, `chain_forecast_execution.py` (span counters on exception paths): #2570 A.
- `services/orchestrator/chain_repository_state.py`, `chain_source_cycle.py`, `scheduler_state_rows.py`, `scheduler_state_identity_filter.py` (if the floor needs it), `chain_forecast_orchestrator_cycle.py` (`_reserve_cycle_stage`, B4), `file_orchestration_journal.py` (`candidate_state` member filter): #2603.
- `services/orchestrator/chain_array_accounting.py`: #2559.
- Runbook: `docs/runbooks/scheduler-dbfree-typed-reasons.md` gains the multi-member cohort permanent-failure row and the new poll-loop error codes.
- Journal content change (B4): downstream-of-forecast model-less cohort master rows gain `cohort_members` (existing field/shape; file journal only). No schema file, DB, or reservation-contract change.
