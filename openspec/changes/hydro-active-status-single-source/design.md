# Design — hydro-active-status-single-source

Fixture level: expanded · repair intensity: standard · line cites against `origin/master` `d7fe213b`.

## Risk triage

Profile triggers: `services/orchestrator`, `run_status`, scheduler state
machine. Item A changes a production dedup decision on the DB lane and three
attempt-scoped write decisions plus one probe on the file-journal lane; B and
C are test-only. Packs selected: **Concurrency / shared state** (a mutable
`set` becomes one shared object across four modules — same shape #1581
accepted for the durable set; no production mutator exists, the parity suite's
sentinel probe is the only writer and it restores under `finally`);
**Legacy compatibility** (`chain.ACTIVE_HYDRO_STATUSES` and
`chain_repository.ACTIVE_HYDRO_STATUSES` stay importable; the compat
re-export list `scheduler_state_compat.py:12` already exports the
`scheduler_state_types` name and is not extended); **Documentation** (the
UNADJUDICATED comments at `scheduler_state_types.py:29-31`,
`chain.py:208-209`, `chain_repository.py:19-20` are replaced by the ruling).
Not selected: Migration (no DDL; `pending` has been an enum member since
`000013`), Security (no new input surface).

## D1 — `"pending"` is an active hydro status; the SQL and journal lanes join the decision lane

Evidence:

| fact | where |
|---|---|
| `pending` is written to `hydro_run.status` only by the two manual-retry submission paths, each from `failed`/`cancelled` and only once the retry job is `submitted`/`running` | SQL lane `retry.py:676-686` (`UPDATE hydro.hydro_run SET status = 'pending' … WHERE status IN ('failed','cancelled')`, guarded by `retry_job.status in {"submitted","running"}`, inside `attempt_manual_retry`; `schedule_auto_retry`'s `"pending"` at `:471` is a `pipeline_job` status); journal lane `file_orchestration_journal.py:11730-11737` `_reset_hydro_run_after_retry_submission`; `000013_enum_remediation.sql:1-3`. No other writer in `services/ workers/ scripts/ db/seeds/` |
| the scheduler decision treats it as active with no stale-placeholder supersede (those rules are scoped to `created/staged/submitted`) | `scheduler_state_decision.py:44-45` imports from `scheduler_state_types`; `:143-160, :182-186, :194` |
| the manual-retry lane treats it as the ACTIVE half of its blocker | `scheduler_state_manual_retry.py:33-34` imports from `scheduler_state_types`; `:524 :729 :885` |
| the SQL probe's hydro arm reads the `pending`-less copy | `chain_repository.py:21, :69, :93` |
| the file journal reads the SAME `pending`-less copy — its import is `from services.orchestrator.chain_repository import ACTIVE_HYDRO_STATUSES` | `file_orchestration_journal.py:70-71`; consumers `:1242` (`has_active_pipeline`), `:3704` (`reject_pipeline_job_submit_attempt`), `:4065` (`permit_pipeline_job_retry`), `:4227` (`demote_operator_verified_reserved_job`), `:4969` (`project_forecast_cohort_tasks`) |
| `chain.ACTIVE_HYDRO_STATUSES` has zero consumers | `grep -rn ACTIVE_HYDRO_STATUSES services/` — `chain.py:210` is definition only; `scheduler.py:74` / `scheduler_state.py:157` re-export the `scheduler_state_types` name |
| no test pins the `pending`-less literal against a module attribute | `tests/test_orchestration_chain.py:6679` pins the SQL parameter literal; `tests/test_production_scheduler.py:34721 :31449`, `tests/test_retry_cancel_consistency.py:1311`, `tests/test_file_orchestration_journal.py:2008 :2045` are fake/helper-local sets or parametrize lists that do not read the modules |
| no journal test exercises a `pending` hydro row at any of the five sites | `uv run pytest -q tests/test_file_orchestration_journal.py` with `pending` added to the shared set in place → 576 passed, 2 skipped (fixture review round 2): green because unpinned, not because unchanged |

Ruling: single definition in `scheduler_state_types`; same-object aliases on
`chain` and `chain_repository`; the journal imports the name from
`scheduler_state_types` directly (task 1.7) so the single source is explicit
rather than transitive. Both the SQL probe and the five journal sites
therefore gain `pending`.

### DB lane — callers of `has_active_pipeline`

- `scheduler_candidates.py:1188` — after the state decision, guarded by
  `candidate_state_scoped_retry_detector`. Not reached for the `pending`
  class: the decision lane already returns `skip` / `active_duplicate_pipeline`
  for a `pending` row (`scheduler_state_decision.py:194`, no supersede rule
  covers `pending`) and the `:1174` skip precedes this probe, so this diff
  changes nothing at this call site.
- `scheduler_candidates.py:406` — only when the active repository has no
  callable `candidate_state`; both production repositories
  (`chain_repository.py:118`, `file_orchestration_journal.py:1517`) and the
  raw-handoff provider (`scheduler_file_providers.py:544`) have one, so this
  arm is reached by test fakes only.
- `chain_forecast_trigger.py:136` — the forecast trigger (orchestrator `trigger_forecast` / `trigger_ready_forecasts` methods; no HTTP/CLI entry and no production caller in-repo — the scheduler dispatches through `orchestrate_cycle`, `scheduler_execution.py:746`) raises already-active
  for a `pending` run.
- `scheduler_backfill_predecessor.py:392-397` — predecessor-cycle skip. This
  emitter runs after the main loop and bypasses the candidate-state decision,
  so it is the one scheduler site whose verdict comes straight from the
  probe: a §8.6 predecessor whose run sits at stale `pending` (no live job)
  is now recorded `skipped / predecessor_backfill_active_pipeline` on every
  pass and the successor's `block_predecessor_pending` never clears, where
  before the change the predecessor was emitted and could be re-orchestrated
  (measured on a real journal repository: HEAD → skipped, base → emitted;
  `failed` control → emitted on both). Pinned by a journal-repository test in
  `tests/test_scheduler_backfill_predecessor.py` (task 1.9).
- `chain_runtime_utils.py:90-123` `_active_orchestration_conflicts`, reached
  from `chain_forecast_control.py:118` `orchestrate_cycle`: a `pending` run
  with no live job now makes `orchestrate_cycle` raise
  `PIPELINE_ALREADY_ACTIVE` for the cycle (candidate-scoped arm `:110-114`
  and cycle-wide arm `:116-120`). Candidate-scoped manual and replacement
  retries are unaffected: `replacement_retry` is computed on that branch
  (`:101-102`) and `if replacement_retry: return False` at `:108-109`
  precedes the probe; `_replacement_retry_scoped_cycle_execution`
  (`:197-199`) short-circuits to `_manual_retry_scoped_cycle_execution`
  (`:156-194`).

Changed input on this lane: exactly `hydro_run.status = 'pending'` with no
non-terminal `pipeline_job` in the cycle matching this candidate's run id, the
cycle run id, or this model (`chain_repository.py:78-91`); a live per-model job
for a sibling model does not mask it, a cycle-scoped job does. While the retry job is live the
UNION arm already returns `True`. The chain-side writers that move a run off
`pending` (`chain_forecast_execution.py:911-925`,
`chain_forecast_orchestrator_cycle.py:833-841`, the unconditional
`update_hydro_run_status` calls at `chain_forecast_trigger.py:170`,
`chain_forecast_execution.py:1127`, `chain_forecast_orchestrator_runtime.py:221
:223`, and `workers/shud_runtime/runtime.py:263-271`'s `ON CONFLICT … SET
status = 'created'`, which fires only once the Slurm job has started) fire
either on the retry job's own progress or on a fresh trigger/orchestration of
the same run id; `reconcile.py` and
`scheduler_gateway.py` never touch `hydro_run`. A retry job that dies at the
Slurm level before any of them runs leaves the row at `pending`: on that
input the decision lane already answers "active", and after this change the
SQL probe does too. Repairing such a row on the SQL lane is a separate
behaviour and stays out of scope. Operator remedies after this change: no
manual-retry *marker* unblocks a `pending` row on the decision lane (the
`:1174` skip precedes the `:1188` escape and the escape needs
`action == "retry"`; `_manual_retry_marker_bound_to_blocker` returns `False`
for an active blocker) — pre-existing and unchanged here.
The reachable stale shape is `hydro_run.status = 'pending'` with every
matching `pipeline_job` terminal. A `pending` row with **no** `pipeline_job`
at all is not production-reachable — both writers create the retry job first
(`retry.py:594-595` raises `RetryNotFoundError` on an empty job list, `:622`
inserts the retry row before the `:676-689` UPDATE; journal `:11707-11708`
upserts the retry job before `_reset_hydro_run_after_retry_submission`) and
nothing in `services/ workers/ scripts/ db/` deletes `ops.pipeline_job` or
`hydro.hydro_run`; D3 constructs that shape by DELETE as a probe input only.
`RetryService.attempt_manual_retry` (`POST /runs/{run_id}/retry`,
`retry.py:546`) repairs the row only when the latest job is failed/cancelled
(`:606-611`), and self-heals it only when the retried stage is the SHUD
forecast (`workers/shud_runtime/runtime.py:264-271`). Manual retry copies
`job_type`/`stage` verbatim with no stage gating (`retry.py:1036-1052`,
`_submit_retry_job:758-784`) and every stage's `pipeline_job` carries the hydro
run id (`chain_forecast_execution.py:1068-1090`), so retrying a failed
`parse_output` job (`chain_stages.py:11`) moves the row to `pending` and its
success leaves it there — `parser.py:38` `PARSE_READY_RUN_STATUSES` excludes
`pending` and `reconcile.py` never writes `hydro_run`. On that row
`_retry_source_job_for_run` (`retry.py:1103-1111`) returns `None` (`pending` ∉
`PARTIAL_OR_FAILED_HYDRO_STATUSES`, `:83`) and retry answers `RETRY_NOT_FOUND`
(`:608-609`). The in-product escape for both shapes is
`POST /runs/{run_id}/cancel` (`apps/api/routes/pipeline.py:584`, same router
as `/retry`): it requires no active job and `_cancel_hydro_run` (`:975-1002`)
writes `cancelled` because `pending` ∉ `_TERMINAL_HYDRO_STATUSES` (`:66`) —
verified by probe on a zero-job `pending` row. `cancelled` ∉
`ACTIVE_HYDRO_STATUSES`, so the refusals below lift; the run then sits in the
pre-existing `cancelled_manual_retry_required` path
(`scheduler_state_failure.py:2166-2180`) — the scheduler does not auto-resume
it, and a further `/retry` re-selects the same failed job and re-wedges on
success unless the retried stage is the forecast. That handling is unchanged
here.
**Re-triggering the cycle (scheduler dispatch only — no operator HTTP/CLI entry), which
repairs such a row today**
(the trigger derives the same `fcst_{source}_{cycle}_{model}` run id,
`chain_forecast_state.py:85`, and `chain_forecast_trigger.py:170` overwrites
the status — it gets through only because the SQL probe at `:136` answers
`False`) **is refused after this change** (`:136` and
`_active_orchestration_conflicts` for `orchestrate_cycle` raise
already-active). That, together with the predecessor-backfill skip above, is the
operator-visible regression class of this change and the PR body states
both (task 4.2).

### File-journal lane — the five sites

- `:1242` `has_active_pipeline`: a candidate-matching `hydro_run` at `pending`
  with no candidate-scoped terminal-success job now answers `True` (the #1472
  suppression still applies and is not mirrored on SQL).
- `:3704` `reject_pipeline_job_submit_attempt`, `:4065`
  `permit_pipeline_job_retry`, `:4227` `demote_operator_verified_reserved_job`:
  each rewrites the cohort members' hydro rows whose `submission_attempt`
  equals the rejected / lost attempt AND whose status is active to `failed`
  (with the path's error code). A `pending` row of that attempt is today
  skipped by the `not in ACTIVE_HYDRO_STATUSES` guard and after this change
  is marked `failed` like its four siblings. This is the journal-lane
  analogue of the SQL-lane stale row: the row is superseded by the attempt's
  outcome instead of surviving it.
- `:4969` `project_forecast_cohort_tasks`: `hydro_is_retryable` now holds for
  a `pending` row, so a reconciled `succeeded` task with a clean or
  reservation-class error code rewrites it to `succeeded` (`:4976-4991`) and a
  reconciled `failed` task rewrites it to `failed` with the task's error code
  or `SLURM_JOB_FAILED` (`:4993-5008`); a `pending` row today is left
  untouched by both branches.

Pins (task 1.8), one per behaviour: journal `has_active_pipeline` on a
`pending` latest view with no jobs → `True`; `permit_pipeline_job_retry` on a
cohort whose member hydro row is `pending` at the lost attempt → that row is
rewritten `failed` / `SLURM_RESERVATION_LOST` (red at `d7fe213b`: the row is
left `pending`); `project_forecast_cohort_tasks` with a `pending` hydro row
and a `succeeded` task → row rewritten `succeeded` (red at `d7fe213b`).
`reject_pipeline_job_submit_attempt` and `demote_operator_verified_reserved_job`
share the guard shape with `permit_pipeline_job_retry`; the parity identity
assertion covers the import, and the `permit` pin is the representative for
the three-site guard.

## D2 — Oracle edits are requirement-driven, recorded, not weakening

- `tests/test_orchestration_chain.py:6679`: `set(parameters[3]) == {...}`
  gains `"pending"`. This assertion is the red proof for the SQL arm (it
  fails at `d7fe213b`+alias, passes after). Recorded in the PR's
  oracle-integrity clause as "assertion changed because the requirement
  changed".
- `tests/test_hydro_status_set_parity.py:146-152`
  `test_active_hydro_status_divergence_is_locked_not_adjudicated` is replaced
  by `test_active_hydro_statuses_are_the_same_object` asserting that
  `chain`, `chain_repository`, `file_orchestration_journal`,
  `scheduler_state_decision` and `scheduler_state_manual_retry` each bind
  `scheduler_state_types.ACTIVE_HYDRO_STATUSES` by identity (the last two are
  the deciding lanes whose "already counts `pending`" is this change's
  premise; re-export surfaces `scheduler.py:74` / `scheduler_state.py:157` /
  `scheduler_state_compat.py:12` are deliberately not pinned) and the
  five-member value; the
  old test going red is the second red proof.
- Two other existing tests gained additive coverage, neither relaxed: task 1.5
  adds `assert journal_module.ACTIVE_HYDRO_STATUSES <= enum_members` to
  `test_every_member_is_a_declared_enum_member_except_complete`
  (`tests/test_hydro_status_set_parity.py`), and task 1.10 widens the two #1472
  foreign-completion matrices (`tests/test_file_orchestration_journal.py:2008
  :2070`) to parametrize over `pending` — rows added, none removed or relaxed.
  Every pre-existing assertion in the journal suite passes unchanged because
  nothing else there pinned `pending` (D1 last row).

## D3 — Real-Postgres proof of the SQL query

A captured-parameter test proves the literal, not the query. A new test in
`tests/test_real_database_integration.py` uses the `throwaway_database_url`
fixture (`tests/conftest.py:181-205`, uuid-named database dropped in
`finally`; the session-scoped `integration_database_url` is shared and
`_clear_issue_126_rows` runs only on the next seed call, so a polluting test
must not use it), applies migrations from zero, and seeds the full FK chain
through `seed_issue_126_data` (`tests/integration_helpers.py:147`: `core.basin`
→ `core.basin_version` → `core.river_network_version` / `core.mesh_version` →
`core.model_instance`, plus `met.data_source`; `hydro.hydro_run` needs
`model_id` and `basin_version_id` NOT NULL per
`db/migrations/000006_hydro.sql:5-6`, `core.model_instance` needs
`river_network_version_id` and `mesh_version_id` per
`000004_core.sql:73-76`; the same recipe as
`tests/test_display_coverage_residual_debt_integration.py:153-155`). It then
sets the seeded `FORECAST_RUN_ID` row to `status = 'pending'` and deletes the
seeded `it126_forecast_job` — the only `ops.pipeline_job` row for cycle
`gfs_2026050300` (`integration_helpers.py:401-424`) — so the cycle has no
`pipeline_job` row at all, and asserts
`PsycopgOrchestratorRepository(throwaway_database_url).has_active_pipeline(source_id=SOURCE_ID, cycle_time=CYCLE_TIME, model_id=MODEL_ID) is True`.
At `d7fe213b` the same test returns `False` (node-27 red proof). Runs on
node-27 only (integration DB fixture); CI has no database for it.

## D4 — Signature pin

`inspect.signature(FileOrchestrationJournalRepository._cycle_rows_by_model_unlocked).parameters`
keys `== ["self", "source_id", "cycle_time", "model_ids"]`, all three
keyword-only, placed next to
`test_sequence_floor_is_exposed_only_as_the_unlocked_variant`
(`tests/test_file_orchestration_journal.py:15222`). Red proof: a scratch copy
with `include_direct_jobs: bool = True` re-added.

## D5 — Sweep hardening (fail closed on rename)

Regexes accept `hydro.run_status`, `"hydro".run_status`, `hydro."run_status"`,
`"hydro"."run_status"` (optional `"` around each segment). A third regex
`ALTER\s+TYPE\s+<ident>\s+RENAME\s+(VALUE|TO)` collects hits; the helper
asserts the list is empty with a message naming the files and saying the
oracle does not model renames. Pinned by a committed test (task 3.4) that repoints `_MIGRATIONS_DIR` at a
`tmp_path` COPY of `db/migrations` plus probe files — the scratch red proofs
below are the same construction run by hand against the old helper. Red proofs
in a scratch migrations dir that is a COPY of `db/migrations`
plus the probe file (the helper's own self-checks — exactly one `CREATE
TYPE`, `succeeded` declared, `pending` added — must keep passing, otherwise
the proof dies at the helper's own `assert len(declaring) == 1` self-check instead of at the assertion it targets): a
quoted-identifier `ADD VALUE 'complete'` (invisible to the old regex, red
under the new), and a `RENAME VALUE 'succeeded' TO 'done'` (silently green
before, red with the fail-closed message after).

## Must-preserve

- Decision-lane and manual-retry-lane answers for every hydro status:
  unchanged (they already read the `scheduler_state_types` set).
- Journal answers for every hydro status other than `pending`: unchanged.
- `ACTIVE_PIPELINE_STATUSES`, `ACTIVE_RETRY_STATUSES`, the durable and
  code-clearing sets: untouched.
- `chain_repository.has_active_pipeline` SQL text: unchanged; only the bound
  parameter list changes. The journal probe's #1472 suppression: unchanged.
- `test_psycopg_has_active_pipeline_includes_queued_pipeline_rows`'s other
  assertions (terminal-status clause, parameter order): unchanged.

## Seams under test

- Same-object identity across the four modules (parity suite).
- SQL parameter capture via `_fetch_optional` stub (existing pattern).
- Real Postgres via `throwaway_database_url` (node-27).
- Journal repository on a `tmp_path` journal root with `_latest_view(...,
  hydro_status="pending")` (existing helper pattern at
  `tests/test_file_orchestration_journal.py:2020-2040`).
- Reducer signature via `inspect`.
- Migration-text sweep via `_MIGRATIONS_DIR` repointing (scratch dir).
