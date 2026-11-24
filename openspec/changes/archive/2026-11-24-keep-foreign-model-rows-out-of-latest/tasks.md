# Tasks

Fixture level: `standard`. Risk packs: read-state contract of the file journal
(fail-closed identity validation), duplicate-submission gate visibility,
derived-view self-consistency.

Must preserve:

- `_job_matches_candidate`, `_filter_cycle_rows_for_model`,
  `_validate_pipeline_job_identity`, `_require_model_id` and the
  candidate-state projection are not edited.
- Every existing test in `tests/test_file_orchestration_journal.py`,
  `tests/test_file_journal_repository_semantics.py`,
  `tests/test_file_orchestration_migration.py` passes unchanged. If an
  existing test pins the behaviour this change removes, stop and report it;
  do not edit it.
- The duplicate-submission gates and the completion gate answer for another
  model's named exact cycle-run row as the frozen #1303 tests state
  (`test_foreign_model_cycle_run_row_stays_visible_to_the_duplicate_submission_gates`,
  `test_foreign_model_completion_row_leaves_the_duplicate_submission_gates_unchanged`).
- A latest view holding a row that names another model with any run id other
  than the exact cycle run id still blocks the read with
  `file_journal_model_mismatch` (field `model_id`), as on master.
- Latest-view payload-level and `hydro_run` model mismatches still block
  (`test_file_orchestration_journal_latest_model_identity_mismatch...` family).
- Master rows of an accepted submission stay out of the latest view as today.
- `_cycle_rows(model_id=B)` still returns another model's named exact
  cycle-run row (pinned by
  `test_full_identity_accessor_rejects_direct_partition_foreign_model_cycle_run_row`);
  the filter lives in the materializer's output, not in `_cycle_rows`.
- `_cycle_rows_by_model_unlocked` (the `include_direct_jobs=False` arm) is not
  edited: it never reads direct records and its journal records are routed by
  model, so it cannot carry another model's row.

## 1. Fix (`services/orchestrator/file_orchestration_journal.py`)

- [x] 1.1 `_materialize_latest_unlocked`: before building the view, compute
  the job ids of rows whose `model_id` is set (not `None` / `""`) and differs
  from the view's `model_id`. Leave those rows out of `pipeline_jobs` and
  leave out, in the same step, every event whose entity type is
  `pipeline_job` (missing entity type counts as `pipeline_job`, as in
  `_event_matches_candidate_rows`) and whose `entity_id` is one of those job
  ids. Compute `replay.job_count` and `replay.event_count` from the lists
  actually written. Applies to both arms (`include_direct_jobs` true/false)
  because the filter runs on the returned rows. Add a short comment naming
  the reader rule it mirrors (`_apply_latest_view` validates every job row
  against the view's model).
- [x] 1.2 `_apply_latest_view`: a job row for which
  `_is_foreign_model_cycle_scope_job(job, source_id=..., cycle_time=...,
  model_id=expected_model_id)` is true is ignored, not trusted: it still has
  to pass every check except model equality -
  `_validate_pipeline_job_identity(job, source_id=..., cycle_time=...,
  model_id=None)`, `_optional_safe_identity(job, "model_id")` and
  `_validate_accepted_submit_evidence(job)` - and is then left out of the row
  set (no upsert). A failing check raises as today. All other rows go through
  the existing validation unchanged. Events are not filtered here. These are
  calls only; the validators themselves are not edited. Add a short comment:
  the shape is the one the pre-#3308 writer emitted; the row's authority is
  its direct record, so the copy in the view is never used.

## 2. Tests (new `tests/test_file_journal_latest_model_scope.py`)

Build journals through the production write API (`upsert_pipeline_job`,
`insert_pipeline_event`) unless a task says a file is hand-written. Use model
B as the candidate and model A as the other model. Read B's latest file from
disk for file-level assertions.

- [x] 2.1 Discriminating pair, red on master: B's own row written, A's named
  row with run id `cycle_<source>_<stamp>` written, B's own row written
  again. Then: B's latest file holds no job with A's job id;
  `candidate_state` for B has no `file_journal` blocker; 
  `query_pipeline_jobs_by_cycle` returns exactly the two real job ids (no
  `file_journal_read_blocked` sentinel); a further `upsert_pipeline_job` for
  a new B job and an `insert_pipeline_event` on B's own job both succeed;
  A's candidate state is unaffected.
- [x] 2.2 Rows and events leave together: as 2.1, plus a hand-written
  model-less `pipeline_event` record on A's job (the manual-retry marker
  shape of #1288: envelope `model_id` null, `entity_type` `pipeline_job`,
  `entity_id` = A's job id) appended to the cycle's `pipeline-events`
  segment before B's second write. Then B's latest file holds no event whose
  `entity_id` is A's job id, and `replay.job_count` / `replay.event_count`
  equal the lengths of the written lists. An event written through
  `insert_pipeline_event` does not reach B's rows (its record is routed by
  A's model), so the hand-written record is the only shape that exercises
  this.
- [x] 2.3 Gate freeze after re-materialization (not on a hand-written direct
  file): 
  - A's named exact cycle-run row active (`status` running or queued, with a
    Slurm job id), B's own row succeeded, B re-materialized: `has_active_pipeline` for B is True,
    `active_slurm_jobs` for B lists exactly A's job id, and B's candidate
    state has no `file_journal` blocker (the True is row visibility, not a
    blocked read).
  - A's named exact cycle-run row a succeeded `state_save_qc` row, B
    re-materialized: `has_completed_pipeline` for B is False and
    `has_active_pipeline` for B is False.
- [x] 2.4 No false exclusion: with a model-less row carrying the exact cycle
  run id, a model-less row carrying a suffixed cycle run id, and B's own
  named row carrying the exact cycle run id, B's re-materialized latest file
  holds all three and B's candidate state lists them.
- [x] 2.5 Polluted view is read and repaired: write B's and A's rows through
  the API, then overwrite B's latest file by hand with its current content
  plus A's row (the shape saved from master in the issue's reproduction).
  Then on a fresh repository instance: `candidate_state` for B has no
  blocker, `query_pipeline_jobs_by_cycle` returns the two real job ids,
  `has_active_pipeline` for B is False (A's row succeeded), a new
  `upsert_pipeline_job` for B succeeds, and B's latest file afterwards holds
  no job with A's job id.
- [x] 2.5b The polluted copy is ignored, not trusted: as 2.5, but the
  hand-written copy of A's row in B's latest disagrees with A's direct
  record. Two directions:
  - copy `running` with a Slurm job id, direct record `succeeded`:
    `has_active_pipeline` for B is False and `active_slurm_jobs` for B is
    empty;
  - copy `succeeded`, direct record `running` with a Slurm job id:
    `has_active_pipeline` for B is True and `active_slurm_jobs` for B lists
    A's job id.
- [x] 2.6 The reader stays strict for every other shape: B's latest file
  hand-written to hold a row naming A with (a) a suffixed cycle run id,
  (b) A's forecast run id, (c) B's forecast run id. Each blocks:
  `candidate_state` for B reports `file_journal_model_mismatch` with field
  `model_id`, and `query_pipeline_jobs_by_cycle` returns the read-blocked
  sentinel. Case (d): a row naming A with the exact cycle run id but a wrong
  `cycle_id` (only `cycle_id` changed; `cycle_time` and `source_id` stay
  correct) blocks with `file_journal_cycle_id_mismatch` (the ignored shape
  is still validated). Parametrized.

## 3. Evidence Floor

- [x] 3.1 Red run: 2.1, 2.2, 2.3 (both bullets: a blocked read answers
  `has_active_pipeline` True), 2.5 and 2.5b fail on the pre-fix tree for the
  stated reason; 2.4 and 2.6 pass there.
- [x] 3.2 Mutations, each killed by the named test and restored:
  - M1 writer filter removed -> 2.1 red.
  - M2 writer filters rows only (events kept) -> 2.2 red.
  - M3 replay counts taken from the unfiltered rows -> 2.2 red.
  - M4 reader skip removed -> 2.5 red.
  - M5 reader skip widened to any row naming another model -> 2.6 red.
  - M6 writer filter widened to drop model-less cycle-scope rows -> 2.4 red.
  - M7 instead of the writer filter, `_filter_cycle_rows_for_model` drops
    other models' named rows -> 2.3 red (`has_active_pipeline` False), and
    the frozen #1303 gate tests red.
  - M8 reader trusts the row instead of ignoring it (validates without the
    model check, then upserts) -> 2.5b red.
  - M9 reader skips before validating -> 2.6 (d) red.
  The file-level assertions of 2.1, 2.2 and 2.4 are what kill M1, M2, M3 and
  M6; they must stay.
- [x] 3.3 Local: `uv run pytest -q tests/test_file_journal_latest_model_scope.py
  tests/test_file_orchestration_journal.py
  tests/test_file_journal_repository_semantics.py
  tests/test_file_orchestration_migration.py`, `uv run ruff check --no-cache .`,
  `openspec validate keep-foreign-model-rows-out-of-latest --strict --no-interactive`.
- [x] 3.4 node-27: the four files of 3.3 plus `tests/test_production_scheduler.py`
  and `tests/test_orchestration_chain.py`, zero unexpected skips.
- [x] 3.5 Production scan result attached to the PR (done before
  implementation: 0 hits on the w3 journal mirror, coverage limits stated).
