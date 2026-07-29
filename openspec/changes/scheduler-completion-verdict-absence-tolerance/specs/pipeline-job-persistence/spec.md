# pipeline-job-persistence — delta for scheduler-completion-verdict-absence-tolerance

## ADDED Requirements

### Requirement: Accepted-submit cohort forecast terminal rows SHALL record init-state identity forward-only

The accepted-submit cohort forecast path SHALL persist the init-state identity (`init_state_id`, `checksum`, `uri`, `valid_time`) **at reservation time**, where the planning context is available, as a per-model identity mapping keyed by `array_task_id`/`model_id` on the cohort master row, outside the cohort-digest input set; terminal per-model row construction SHALL read each row's identity from the master-row mapping by its own `array_task_id` rather than from cohort-member projection, and a scalar single-identity field SHALL NOT be used. The recording SHALL NOT alter the ordinary-upsert frozen-field semantics (the identity's value is stable from reservation onward) and SHALL NOT enter the cohort-digest member field set — historical rows' `forecast_cohort_digest` validation results SHALL be unchanged. Invalid or partial identity payloads SHALL be rejected by accepted-submit normalization. Existing journal rows without these fields SHALL remain readable unchanged — no migration, no backfill, no rewrite of historical rows.

#### Scenario: New cohort terminal rows carry the identity

- **WHEN** a cohort forecast job reserved after this change reaches a terminal status through the accepted-submit path
- **THEN** its journal row records the init-state identity captured at reservation time

#### Scenario: Historical cohort digests are untouched

- **WHEN** normalization validates a pre-change cohort row's `forecast_cohort_digest` after this change is deployed
- **THEN** the validation result is identical to before this change

#### Scenario: Legacy rows stay untouched and readable

- **WHEN** the journal contains pre-change cohort rows without init-state fields
- **THEN** readers treat the record as absent without error and no writer mutates those rows

#### Scenario: Invalid identity is rejected by the invariant gates

- **WHEN** an upsert presents an init-state identity payload with a malformed or partial field set
- **THEN** accepted-submit normalization rejects the transition rather than persisting a partial record
