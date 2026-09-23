## MODIFIED Requirements

### Requirement: The manual-retry preview points a refused hydro run at its cohort master

When the node-22 manual-retry script previews a hydro run id and the answer is `no_retryable_failed_job`, the preview SHALL list, read-only, every cohort master row of the same cycle whose `state_save_qc` job is in a failed status and whose recorded cohort membership includes the run's model, with its run id, job id, status, error code, and member count, together with a warning that marking it re-runs the whole cohort from convert. A failed master SHALL NOT be listed when a later `state_save_qc` row of a `cycle_<source>_<stamp>_*` cohort run of the same cycle has succeeded and provably covers the same model under the same membership rule that decides listing (a single-model row by its `model_id`; a model-less row only through complete recorded `cohort_members`); "later" is ordered by the journal's retry truth sort key, and a row whose coverage cannot be proven supersedes nothing. The selector's refusal, the script's exit codes, and every other preview answer SHALL be unchanged, and a failure of the hint query SHALL NOT change the preview decision.

#### Scenario: forecast succeeded and the cohort master's state_save_qc permanently failed

- **WHEN** the operator previews `fcst_ifs_2026092212_<model>`, whose hydro run succeeded, and the single-model cohort master `cycle_ifs_2026092212_convert_<model>` has a `permanently_failed` `state_save_qc` row while a multi-model cohort of the same cycle has a succeeded one
- **THEN** the preview is refused with `no_retryable_failed_job` and lists exactly the single-model cohort master with `member_count` 1 and the whole-cohort warning
- **THEN** previewing the listed cohort run id yields `would_mark` for its `state_save_qc` job

#### Scenario: no failed cohort covers the model

- **WHEN** no cohort master of the cycle covering the model has a failed `state_save_qc` row
- **THEN** the preview is refused as before and lists no cohort candidates

#### Scenario: a master recovered under a new cohort run id is not listed

- **WHEN** the failed master `cycle_ifs_2026092212_convert_<model>` has a `permanently_failed` `state_save_qc` row and a later `cycle_ifs_2026092212_full_<model>` cohort has a succeeded `state_save_qc` row carrying the same `model_id`
- **THEN** previewing `fcst_ifs_2026092212_<model>` is still refused with `no_retryable_failed_job`
- **AND** `cohort_candidates` is empty and no `warning` is emitted

#### Scenario: a failure after the last success is still listed

- **WHEN** the covering succeeded `state_save_qc` row is earlier than the failed master's latest `state_save_qc` row
- **THEN** the failed master is listed in `cohort_candidates`

#### Scenario: a later success whose coverage cannot be proven supersedes nothing

- **WHEN** a later model-less succeeded `state_save_qc` row belongs to a cohort whose recorded membership is incomplete (for example a blank member `model_id`)
- **THEN** the failed master is still listed with its `member_count` and the whole-cohort warning
