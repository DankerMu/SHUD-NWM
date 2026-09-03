# Adjudicate the hydro active-set `pending` divergence and close the two parity-lock gaps left by #1995

## Why

PR #1995 (issues #1581 #1659 #1661 #1762 #1763) merged with three recorded
residuals (its 偏离 2 and 已知限制; line cites below are against
`origin/master` `d7fe213b`, symbol names are authoritative):

- **A. `ACTIVE_HYDRO_STATUSES` has three copies and two lanes read the wrong
  one.** `scheduler_state_types.py:32` holds `{"created","staged","pending",
  "submitted","running"}`; `chain.py:210` and `chain_repository.py:21` hold the
  same set minus `"pending"`. #1581 locked the divergence as UNADJUDICATED
  because its author ruled the `pending` question a behaviour call for a
  separate change. The audit is now done (design D1). `"pending"` is the
  status manual retry writes to `hydro_run` once the retry job is submitted
  (SQL lane `retry.py:676-686`; journal lane
  `file_orchestration_journal.py:11730-11737`; the reason `000013` added the
  enum member). The consumers that reason about "is this hydro run in flight"
  split by which copy they import: the scheduler candidate decision
  (`scheduler_state_decision.py:194`) and the manual-retry blocker lane
  (`scheduler_state_manual_retry.py:524 :729 :885`) read the
  `scheduler_state_types` set and count `pending`; the SQL
  `has_active_pipeline` hydro arm (`chain_repository.py:62-96`) and the file
  journal — which imports the name from `chain_repository`
  (`file_orchestration_journal.py:70-71`) for its `has_active_pipeline`
  (`:1242`), three attempt-scoped write paths (`:3704 :4065 :4227`) and the
  cohort projection (`:4969`) — read the `pending`-less literal.
  `chain.py:210` has no consumer at all. The literal predates `000013` and was
  never updated: drift, not intent.
- **B. The "reducer has no flag" SHALL has no executable pin.** The archived
  `pipeline-job-persistence` requirement says `_cycle_rows_by_model_unlocked`
  SHALL have no direct-record flag, but unlike `_next_sequence` (pinned by
  `hasattr`) a re-added `include_direct_jobs` kwarg passes every scenario.
- **C. The parity enum sweep is blind to two SQL forms.**
  `tests/test_hydro_status_set_parity.py:30-37` matches only the unquoted
  `hydro.run_status` identifier, and nothing in the sweep notices an `ALTER
  TYPE hydro.run_status RENAME VALUE`, so a rename would leave the oracle
  asserting a member that no longer exists.

## What changes

1. **A — single definition, behaviour change on the DB lane and the
   file-journal lane.** `scheduler_state_types.ACTIVE_HYDRO_STATUSES` becomes
   the one definition; `chain.ACTIVE_HYDRO_STATUSES` and
   `chain_repository.ACTIVE_HYDRO_STATUSES` become same-object aliases (names
   kept, mirroring #1581's `COMPLETED_HYDRO_STATUSES`), and the file journal
   imports the name from `scheduler_state_types` directly. Consequences,
   stated plainly (design D1 enumerates every site):
   - SQL `has_active_pipeline` hydro arm matches `pending`. While a retry job
     is live the `pipeline_job` UNION arm already answered `True`, so the
     only input whose answer changes is a `hydro_run` at `pending` whose
     cycle has no non-terminal `pipeline_job`; the decision lane already
     answers "active" there.
   - Journal `has_active_pipeline` matches `pending` (still subject to its
     own #1472 terminal-completion suppression, which is not mirrored on SQL).
   - Journal write paths: a `pending` row of the rejected / lost attempt is
     now marked `failed` by `reject_pipeline_job_submit_attempt`,
     `permit_pipeline_job_retry` and `demote_operator_verified_reserved_job`
     exactly as `created`/`staged`/`submitted`/`running` rows are, and
     `project_forecast_cohort_tasks` now treats a `pending` row as retryable
     so a reconciled succeeded task rewrites it to `succeeded`. Today those
     paths skip a `pending` row, which is how a stale `pending` row survives
     on the journal lane.
   The parity lock's divergence test is replaced by identity assertions; the
   four-member literal pinned at `tests/test_orchestration_chain.py:6679`
   gains `"pending"` (an oracle edit driven by the requirement change,
   recorded as such); the journal answers on a `pending` row get pins; a
   real-Postgres test proves the SQL query, not just the parameter.
2. **B — signature pin.** `inspect.signature` of the reducer is pinned to
   `(self, *, source_id, cycle_time, model_ids)` next to the `_next_sequence`
   `hasattr` pin.
3. **C — sweep hardening.** Both parity regexes accept optionally
   double-quoted identifiers on either segment (`"hydro"."run_status"`), and
   the sweep fails closed — with a message naming the file — on any `ALTER
   TYPE hydro.run_status RENAME VALUE` or `RENAME TO`, because the oracle does
   not model renames and must not silently keep asserting a renamed member.

`design.md` is present (fixture level `expanded`: profile trigger
`orchestrator` / `run_status`, and item A changes production dedup and
journal write decisions).

## Non-goals

- Repairing a stale `pending` `hydro_run` on the SQL lane (a retry job that
  dies at the Slurm level before any chain-side writer runs never rewrites the
  row). The decision lane already treats such a row as active; this change
  makes the SQL probe consistent with it and records the repair as a separate
  behaviour (D1). On the journal lane the attempt-scoped write paths now
  supersede such a row on the next job outcome, which is a consequence, not a
  goal.
- The terminal-completion suppression asymmetry between the journal probe
  and the SQL probe (#1472; `file_orchestration_journal.py:1221-1243`).
- `ACTIVE_PIPELINE_STATUSES`, `ACTIVE_RETRY_STATUSES`, the durable-success and
  code-clearing sets, any `ALTER TYPE`.
- Modelling `RENAME VALUE` in the sweep (fail closed, do not model).

## Issues

No new issue is filed (standing user instruction for this batch: resolve
in-batch, do not scribe). Origin: PR #1995 偏离 2 and 已知限制; #1581's
"Out of scope" note explicitly routed the `pending` behaviour call to a
separate change — this is that change.
