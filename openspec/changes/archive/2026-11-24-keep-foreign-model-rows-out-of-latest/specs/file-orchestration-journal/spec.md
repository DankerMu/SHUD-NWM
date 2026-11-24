## ADDED Requirements

### Requirement: A model's latest view SHALL carry only rows its own reader accepts

The latest materializer and the latest reader SHALL agree on which job rows belong in
`latest/<source>/<stamp>/<model>.json`:
- **Writer:** a job row whose `model_id` is set and names a model other than the view's model SHALL NOT be written into
  that view, and the `pipeline_job` events of such a row SHALL be left out in the same step. The view's replay
  `job_count` and `event_count` SHALL equal the number of job rows and events written.
- **Reader:** a job row in a latest view that names another model and carries the exact cycle run id
  `cycle_<source>_<stamp>` SHALL be ignored: it does not block the read and does not enter the row set from that view.
  The ignored row SHALL still pass every identity and field check other than model equality; this is the only
  exception to "Trusted read surfaces validate schema and identity before replay". Any other row that names another
  model SHALL still fail closed as `file_journal_model_mismatch` or the applicable identity blocker.
- **Gate visibility:** the duplicate-submission gates (`has_active_pipeline`, `active_slurm_jobs`) and the completion
  gate (`has_completed_pipeline`) SHALL answer for another model's named exact cycle-run row exactly as before this
  requirement; the row reaches them through its direct record.
- **Kept rows:** model-less cycle-scope rows (exact and suffixed cycle run id) and the view model's own rows, including
  its own named exact cycle-run row, SHALL still be written into the view.

#### Scenario: Re-materializing a model beside another model's cycle-run row keeps the model readable

- **WHEN** model A has a named row with run id `cycle_<source>_<stamp>` and model B's own row is written again
- **THEN** B's latest view holds no row naming A and no `pipeline_job` event of that row
- **AND** B's candidate state carries no file-journal blocker, a cycle-level job query returns the real rows of both
  models, and a further job write and event write for B succeed

#### Scenario: The gates still see the other model's row

- **WHEN** model A's named exact cycle-run row is active and model B's latest view has been re-materialized
- **THEN** `has_active_pipeline` for B is True and `active_slurm_jobs` for B lists A's row, while B's candidate state
  is not read-blocked
- **AND** when A's named exact cycle-run row is a succeeded completion-stage row, `has_completed_pipeline` for B is
  False

#### Scenario: A latest view polluted by the earlier writer is read and repaired

- **WHEN** B's latest view already holds A's named exact cycle-run row
- **THEN** reads for B and cycle-level reads are not blocked, a write for B succeeds, and the rewritten view no longer
  holds A's row
- **AND** while the polluted view exists, the gates answer from A's direct record, not from the copy in B's view

#### Scenario: Any other foreign row in a latest view still blocks

- **WHEN** B's latest view holds a row naming A whose run id is a suffixed cycle run id or A's forecast run id
- **THEN** the read is blocked with the same identity blocker as before this requirement
- **AND** a row naming A with the exact cycle run id but a wrong `cycle_id` is blocked as
  `file_journal_cycle_id_mismatch`
