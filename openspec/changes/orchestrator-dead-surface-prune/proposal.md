# Prune five zero-caller orchestrator surfaces and single-source the hydro status sets

## Why

Five independent read-only findings, all `p3` tech-debt, all in
`services/orchestrator`, all pre-existing at `origin/master` `4f3fd89a`
(line cites below are against that head; symbol names are authoritative):

- **#1661** `_cycle_rows_by_model_unlocked` (`file_orchestration_journal.py:5848`)
  takes `include_direct_jobs: bool = True` (`:5854`); every caller — five
  direct production sites (`:2397 :3690 :4052 :4215 :4903`), the
  `_materialize_latest_unlocked` routing arm (`:9951`) and both tests — passes
  `False`. The `True` arm (`:5903-5910` direct-job collection, `:5916-5921`
  the in-guard `_cache_cycle_rows` store with `fingerprint=None`) never runs.
  A future default-value caller would bypass the `_cycle_rows` fingerprint
  discipline (#1595/#1600 family).
- **#1659** `_next_sequence` (`:9603-9605`) is a `_write_lock`-taking wrapper
  over `_next_sequence_unlocked` with zero production callers (ten write-lane
  sites call the unlocked variant while already holding the lock); five tests
  (`tests/test_file_orchestration_journal.py:12701 :12726 :12738 :12766 :12771`)
  are its only users. `threading.Lock` is not re-entrant, so the wrapper is a
  deadlock trap for any future caller inside the write lane.
- **#1762** `_cycle_scope_from_file_run_id` (`:12647`) tries the
  `analysis_{src}_{start}_{end}_{model}` shape (`:12672-12677`, import `:129-131`)
  after forecast/cohort. Its only consumers are the `pipeline_job` lookups
  (`query_pipeline_jobs_by_run` `:1867`, `_cycle_scope_from_idempotency_key`
  `:12751`), and `_validate_pipeline_job_identity` (`:14320`) rejects an
  `analysis_*` run id in **both** of its branches (`:14355-14366` with model id,
  `:14367-14377` without) with `file_journal_run_mismatch`, on every write and
  read path. No such row can exist; the branch is dead and its docstring
  (`:12655-12663`) describes it as live.
- **#1763** `reservation.candidate_idempotency_key` (`reservation.py:67-76`) has
  zero callers repo-wide and mints `source:cycle:basin:stage`, which diverges
  from the production shape `run_id:stage[:suffix]`
  (`chain_runtime_utils._cycle_stage_idempotency_key` `:462`) that
  `_cycle_scope_from_idempotency_key` depends on; the module comment
  (`reservation.py:45-46`) documents the dead shape as the real one.
- **#1581** the hydro durable-success set `{"succeeded","parsed","published","complete"}`
  exists as three named literal copies (`scheduler_state_types.py:35`,
  `chain.py:208`, `chain_repository.py:19`) plus one anonymous inline
  (`scheduler_state_failure.py:149`) with no parity test, and the sibling
  `ACTIVE_HYDRO_STATUSES` has already drifted (`scheduler_state_types.py:29`
  holds `"pending"`, `chain.py:207` / `chain_repository.py:18` do not). The
  six-member error-code-clearing set is likewise duplicated
  (`scheduler_state_failure.py:321` named, `file_orchestration_journal.py:2485`
  inline).

## What changes

1. **#1661** `include_direct_jobs` is removed from `_cycle_rows_by_model_unlocked`
   together with the direct-job ternary and the in-guard store; the six
   in-module callers and the two tests stop passing it.
   `_materialize_latest_unlocked` keeps its own `include_direct_jobs`
   parameter (its `True` arm routes to live `_cycle_rows`); of its nine
   callers, the three that pass `include_direct_jobs=False` (`:3771 :4111 :4323`)
   keep doing so verbatim and the six that take the default `True`
   (`:5202 :9041 :9181 :9227 :9427 :9998`) are untouched — flipping either
   group would change which arm the latest view is built from.
2. **#1659** `_next_sequence` is deleted; the five tests call
   `_next_sequence_unlocked` with unchanged assertions.
3. **#1762** ruling: unreachable. The analysis branch and the
   `_ANALYSIS_RUN_ID_RE` import are deleted, the docstring rewritten; the
   ruling is pinned by tests on the validator (both branches reject) and on
   the derivation (`analysis_*` → `None`, i.e. fall-open). `run_identity.py`'s
   regex and `parse_run_cycle` are untouched (retention consumes them).
4. **#1763** `candidate_idempotency_key` is deleted; the module docstring
   (`:21-24`, which spelled the deleted shape) and the charset comment
   (`:45-46`) both describe `run_id:stage[:suffix]` with its producer, and
   the `chain_runtime_utils.py:472` mention of the phantom shape is dropped
   (round-2 cand-07).
5. **#1581** `scheduler_state_types.DURABLE_HYDRO_SUCCESS_STATUSES` becomes the
   single definition; `chain.COMPLETED_HYDRO_STATUSES` and
   `chain_repository.COMPLETED_HYDRO_STATUSES` are same-object aliases (names
   kept: `chain_forecast_trigger.py:31` reads by `getattr`, the compat list
   `scheduler_state_compat.py:20` and `scheduler.py:82` /
   `scheduler_state.py:165` import by name); the inline at
   `scheduler_state_failure.py:149` uses the shared name. `"complete"` is
   **kept** (ruling in design D5: dead on the DB lane, unvalidated and
   test-face-reachable on the journal lane; removal flips 13 tests and the
   issue's own acceptance demands verbatim-identical decisions). The
   code-clearing set gets one definition in `scheduler_state_types`
   (`HYDRO_RUN_CODE_CLEARING_STATUSES`) with `_HYDRO_RUN_CODE_CLEARING_STATUSES`
   kept as an alias in `scheduler_state_failure` and the journal inline
   replaced by the import. A new parity test pins same-object identity,
   enum membership (with `"complete"` as the one named exception), and the
   `ACTIVE_HYDRO_STATUSES` `"pending"` divergence as explicitly unadjudicated.
   The #1155 wording at `scheduler_state_types.py:30-34` and the
   `tests/test_retry.py:164-176` docstring is corrected per the issue's
   second comment; the assertions at `:178-181` stay true and unchanged.

`design.md` is present (fixture level `expanded`, profile trigger
`orchestrator` / `run_status` / state machine; concurrency pack selected for
the journal cache store and lock wrapper).

## Non-goals

- Any behavior change: no decision, query, error, or cache result differs
  for any input either lane can produce today.
- `_cycle_rows`' live store (`:5843`), `_cycle_rows_cache` consumers,
  `_direct_pipeline_job_records_for_cycle_cached`, `_materialize_latest_unlocked`'s
  routing flag.
- `_next_sequence_unlocked`, `_write_lock`, the `_write_lock.locked()` window
  probe (#1595).
- `run_identity.ANALYSIS_RUN_ID_RE`, `parse_run_cycle`, `retention.py`;
  loosening `_validate_pipeline_job_identity` to admit analysis rows.
- `IDEMPOTENCY_KEY_RE`, `slurm_comment_for`, `idempotency_key_from_comment`,
  `validate_idempotency_key`.
- Adjudicating the `ACTIVE_HYDRO_STATUSES` `"pending"` divergence (locked and
  labelled, not changed); cycle-domain sets (`TERMINAL_PIPELINE_SUCCESS_STATUSES`,
  `RAW_MANIFEST_READY_CYCLE_STATUSES`, `_TERMINAL_FORECAST_CYCLE_SUCCESS_STATUSES`);
  `MANUAL_RETRY_DURABLE_SUCCESS_STATUSES` (#1155); any `ALTER TYPE`.
- Editing archived OpenSpec changes (the `_next_sequence` mention at
  `openspec/changes/archive/2026-08-18-journal-containment-aware-existence-probe/tasks.md:35`
  is history and stays).

## Issues

Closes #1581, closes #1659, closes #1661, closes #1762, closes #1763 — one
PR by user choice. None carries an upstream `Suggested fixture level` or
`Minimal mergeable slice`; triaged here as `expanded`.
