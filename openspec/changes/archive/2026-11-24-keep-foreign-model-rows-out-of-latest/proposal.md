# Keep another model's rows out of a model's latest view

## Why

The file journal's latest materializer writes rows that its own latest reader
refuses (#3308).

- `_cycle_rows(model_id=B)` returns every row `_job_matches_candidate` admits
  for B. That predicate is deliberately wide: a row that names model A and
  carries the exact cycle run id `cycle_<source>_<stamp>` is admitted, because
  the duplicate-submission gates must keep seeing it (#1288 / #1303 D1).
- `_materialize_latest_unlocked` writes that row set unchanged into
  `latest/<source>/<stamp>/B.json`.
- `_apply_latest_view` validates every job row of that file against the file's
  model and raises `file_journal_model_mismatch` on the row that names A.

After one re-materialization of B's own rows, every read of that
(model, cycle) is blocked, `has_active_pipeline` answers True because the read
is blocked, cycle-level job queries return the read-blocked sentinel, and every
later write for B fails inside the materializer's own read. The file cannot
heal itself.

Reproduced on master `4762ba429` through the production write API
(`upsert_pipeline_job` three times). Read-only scan of the production journal
mirror on 2026-11-24 (7705 direct rows, 20594 latest files, cycles
2026062118..2026112400): no row of this shape and no polluted latest file.
The current scheduler does not produce the row shape; historical import and
non-scheduler entry points can.

## What Changes

- Writer: a latest view no longer carries a job row that names a model other
  than the view's model, and no longer carries the `pipeline_job` events of
  such a row. The view's replay counts describe what was written.
- Reader: a latest view that already holds another model's named exact
  cycle-run row (the one shape the writer produced) is read with that row
  ignored instead of blocking the read. The ignored row is still checked for
  everything except model equality. Every other identity mismatch in a
  latest view still fails closed. A polluted view is rewritten clean by the
  next write for that model; a cycle that never gets another write keeps the
  old file, readable.
- No change to `_job_matches_candidate`, `_filter_cycle_rows_for_model`,
  `_validate_pipeline_job_identity`, the candidate-state projection or any
  gate surface. The other model's row keeps reaching the gates through the
  direct `pipeline-jobs/<job_id>.json` lane, which never depended on the
  latest file.

## Why the reader ignores instead of trusting

The ignored row contributes nothing from the latest file: the row's authority
stays with its direct record and its owner's journal records, both written by
the same call that created it (`_write_pipeline_job_unlocked`, also used by
`append_historical_pipeline_job`). Ignoring is narrower than accepting, and it
is limited to the shape the old writer emitted, so a latest view edited to
carry any other foreign row is still refused. The next write for that model
rewrites the view without the row.

The reader does not drop events of the ignored row: events in a latest view
never caused the refusal, and `_event_matches_candidate_rows` already keeps or
drops a `pipeline_job` event by whether its row is in the row set, identically
for events read from a latest view and from the event segments.

## Impact

- Code: `services/orchestrator/file_orchestration_journal.py`
  (`_materialize_latest_unlocked`, `_apply_latest_view`).
- Tests: new `tests/test_file_journal_latest_model_scope.py`.
- Spec: `file-orchestration-journal` (one added requirement).
- Operations: no host change, no journal migration. Deploy rides the next
  compute-checkout update.
