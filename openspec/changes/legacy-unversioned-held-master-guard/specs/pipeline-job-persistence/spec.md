## ADDED Requirements

### Requirement: No journal writer persists a reserved unversioned forecast cohort master

The file-journal store SHALL refuse to persist a `pipeline_job` row that is `reserved`, has a forecast cohort stage (`stage`, or `job_type` when `stage` is empty, is `forecast`, `run_shud_forecast`, or `run_shud_forecast_array`), carries non-empty `cohort_members`, and does not carry the current accepted-submit contract version. The refusal SHALL be the named error `file_journal_legacy_unversioned_reserved_forecast_master` and SHALL leave the journal byte-identical. It SHALL be checked before any side effect of the write (including a reconcile-inventory anchor) and at the record-level append funnel that every durable `pipeline_job` append passes through, so it applies to upsert, reservation, reservation reclaim (of a dead legacy forecast master or of an auto-retry clone of one), batch appends, and historical append. The historical scheduler-state import SHALL check its job rows before creating the journal root or writing any record. Rows that differ in any of the four properties SHALL be written as before. A matching row that already exists in the journal SHALL stay readable, and SHALL keep its fail-closed reconcile (`legacy_unversioned_read_only`), operator-bind refusal (`legacy_unversioned_unsupported`), and `escalate` listing.

#### Scenario: A reclaim of a dead legacy forecast master is refused
- **WHEN** `reclaim_pipeline_job_reservation` is asked to move a dead legacy unversioned forecast cohort master with `cohort_members` back to `reserved`
- **THEN** it SHALL raise `file_journal_legacy_unversioned_reserved_forecast_master`
- **AND** the journal SHALL be byte-identical

#### Scenario: The historical import fails closed on a reserved legacy forecast master
- **WHEN** the historical scheduler-state import meets a reserved unversioned forecast cohort master
- **THEN** it SHALL fail with `file_journal_legacy_unversioned_reserved_forecast_master` before creating the journal root or writing any record

#### Scenario: Legitimate writes are unchanged
- **WHEN** a versioned forecast cohort master is reserved, or a legacy forecast row is written in a non-`reserved` status, or a forcing or non-forecast legacy row is reserved
- **THEN** the write SHALL succeed exactly as before
