# Tasks — cohort-chain-silent-stall (#2570 group A, #2603, #2559)

## Risk packs

- Public API / CLI / script entry — not selected: no entrypoint change.
- Config / project setup — not selected.
- File IO / path safety / overwrite — not selected: journal writes use existing writers.
- Schema / columns / units / field names — selected: projection `restart_stage` enum; new error code string → C tests + typed-reasons runbook (4.1).
- Auth / permissions / secrets — not selected.
- Concurrency / shared state / ordering — selected: shared cycle-scope cohort rows across sibling cohorts; poll loop vs persisted row state → B sibling test (2.4), A2 no-resubmit (1.3).
- Resource limits / large input — selected (light): B4 adds members to downstream cohort rows (journal size) and B1 adds a per-candidate pass over cycle rows → note per-row bytes and keep compaction stripping members (2.4).
- Legacy compatibility / examples — selected: historical rows without `cohort_members`, single-model cohorts → 2.5, 2.6.
- Error handling / rollback / partial outputs — selected: #2570 A itself → 1.x.
- Release / packaging — not selected.
- Documentation / migration notes — selected: `docs/runbooks/scheduler-dbfree-typed-reasons.md` → 4.1.

## 1. #2570 group A — poll loop isolation (`chain_stage_execution.py`, `chain_forecast_execution.py`)

- [x] 1.0 Record in the PR that A is hardening. The node-22 evidence that could name the raise point was purged, and the 2026-09-26 scan found zero traceback tails.
- [x] 1.1 A1: catch `get_job_status` failures and keep polling. If the deadline arrives after ≥1 query failure, end with `reconcile_unverified`/`SLURM_STATUS_QUERY_UNAVAILABLE` for every stage, with no terminal write and the Slurm id still bound. With zero failures, keep today's timeout path.
- [x] 1.2 A3: catch an `insert_pipeline_event` failure after a successful status write, count it, warn, and continue.
- [x] 1.3 A2: when `_update_runtime_pipeline_status` fails with anything other than a conflict, set the persist-failure marker on the `TerminalJobObservation`. Both callers then skip every post-poll write and gateway call, and return `reconcile_unverified`/`STAGE_RUNTIME_STATUS_PERSIST_FAILED`. Nothing is resubmitted.
- [x] 1.4 A4: make the span `basin_count` real on every exit path by setting it at stage entry (the terminal populate overwrites it with the same value). Unexpected exceptions still propagate unchanged.
- [x] 1.5 Audit `chain_forecast_execution._poll_until_terminal` (~1658). Apply A1–A3 there, or record why not.
- [x] 1.6 Tests: A1 (transient, then terminal; convert outage until the deadline, submit count unchanged; zero-failure timeout unchanged); A2 across two passes (fake raises on every write in pass 1, healed in pass 2; members reconciling; submit count unchanged); A3; A4.

## 2. #2603 — recorded-membership attribution (`file_orchestration_journal.py`, `chain_repository_state.py`, `chain_source_cycle.py`, `scheduler_state_rows.py`, `scheduler_state_identity_filter.py`, downstream cohort row writer)

- [x] 2.1 B1: in file-journal `candidate_state`, classify rows before `_compact_cycle_scope_job` using `_complete_cohort_members_by_run`, the same rule as `has_active_pipeline`. Add the class as a projection key, drop `non_member` rows, and have downstream consumers read the annotation without recomputing it.
- [x] 2.2 B2: `member` rows count on the failure side, the identity filter, the attempt floor and decision authority. B2b: a permanently failed `incomplete` row gives `blocked/cohort_membership_unprovable`.
- [x] 2.3 B3: `member` and `unwitnessed` rows keep crediting success; record the unwitnessed asymmetry as a deviation.
- [x] 2.4a Row-level classification first for rows carrying their own `cohort_members`; run-level union only for rows without; `has_active_pipeline` unchanged.
- [x] 2.4b B2b precedence/override/exit: same exits as `permanent_failure_guard`, evaluated just before the completed-stage resume (otherwise the unattributed failure takes that silent resume); own terminal success wins; manual-retry marker clears; tests.
- [x] 2.4 B4: model-less cohort master rows of downstream-of-forecast stages record `cohort_members` at creation in `chain_forecast_orchestrator_cycle._reserve_cycle_stage`, members only (no accepted-submit markers), gated on `supports_accepted_submit_reconcile`; test that B4 rows + `_retry_N` rows behave identically under journal validation / restart-reconcile inventory / upsert merge. Convert is out of scope (#2546, S2).
- [x] 2.5 Tests through real `orchestrate_cycle` + `FileOrchestrationJournalRepository` + `_candidate_state_decision`, with run ids from the real `candidate_execution_cohort_run_id`:
  - 3-member permanent failure → blocked/permanent_failure_guard;
  - transient failures across ≥2 passes: attempt increments and stops at the budget;
  - a strict-subset `state_save_qc` restart cohort: success is credited and not resubmitted, and a permanent failure blocks exactly its members;
  - a sibling non-member is unaffected;
  - partial forcing: only surviving members are attributed;
  - incomplete membership + permanent failure → `cohort_membership_unprovable`;
  - an unwitnessed historical row matches origin/master.
- [x] 2.6 Single-model cohort (existing `tests/test_state_save_submit_ambiguity.py`), #2584 ambiguous auto-retry, and the DB-path decision tests are unchanged, and the DB query is not widened.

## 3. #2559 — projection restart_stage (`chain_array_accounting.py`)

- [x] 3.1 failed/unverified → `"forecast"`; succeeded → `"state_save_qc"`.
- [x] 3.2 Test: FileJournal `record_cycle_stage_status_override` with basins `restart_stage` convert / forcing (parametrized), one failed task → projection persisted `restart_stage=="forecast"`, master not deferred; a missing task persists nothing and yields governed `accounting_unavailable`; red before fix shows `identity_mismatch_blocked`.

## 4. Docs

- [x] 4.1 `docs/runbooks/scheduler-dbfree-typed-reasons.md`: rows for `STAGE_RUNTIME_STATUS_PERSIST_FAILED`, `SLURM_STATUS_QUERY_UNAVAILABLE` (both reconcile_unverified, non-resubmitting, resolved next pass), `cohort_membership_unprovable` (blocked; manual-retry clears), and the multi-member cohort permanent-failure decision row.

## Evidence Floor

- Local: new tests red on origin/master source, green after; `uv run ruff check .`; `openspec validate cohort-chain-silent-stall --strict --no-interactive`; `uv run pytest -q tests/test_state_save_submit_ambiguity.py tests/test_orchestration_chain.py tests/test_production_scheduler.py tests/test_select_ci_tests.py` + any file touching the changed modules.
- node-27 (`TMPDIR=/home/nwm/tmp`, umask 0002): the same suites + new tests green.
- #2570 AC "node-22 live receipt" for group A (a real pass with a split cohort writing all stage rows) is post-merge/deploy-gated; record as pending in the PR.
