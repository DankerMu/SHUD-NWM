## ADDED Requirements

### Requirement: Historical No-Curve Cleanup Is Auditable And Safe By Default

The system SHALL provide an operator-facing cleanup command for historical
`flood.return_period_result` rows where `return_period IS NULL`,
`warning_level IS NULL`, and `quality_flag` is `no_frequency_curve` or
`no_usable_frequency_curve`.

#### Scenario: Dry-run manifest does not mutate database

- **WHEN** the cleanup command is run without explicit apply mode
- **THEN** it SHALL produce a manifest with candidate counts and affected runs
- **AND** it SHALL delete zero rows

#### Scenario: Apply deletes only preserved-quality no-curve candidates

- **WHEN** apply mode is enabled with a bounded batch size
- **AND** every affected run has explicit `flood.run_product_quality`
- **THEN** each batch SHALL delete only rows matching the no-curve null predicate
- **AND** deletion SHALL recheck the same filters and candidate predicate used
  by dry-run summaries
- **AND** batch ordering and resume evidence SHALL use the stable row identity
  tuple `(run_id, river_network_version_id, river_segment_id, duration,
  valid_time, max_over_window)`
- **AND** rows with non-null `return_period` or non-null `warning_level` SHALL
  remain
- **AND** affected run quality summaries SHALL remain present after cleanup

#### Scenario: Missing explicit quality blocks apply

- **WHEN** candidate rows exist for a run without `flood.run_product_quality`
- **THEN** apply mode SHALL fail before deletion
- **AND** the manifest or error SHALL identify the missing quality run
- **AND** no force or override option SHALL allow deletion for that run

#### Scenario: Filters define one candidate set

- **WHEN** the cleanup command is run with any combination of `run_id`,
  `basin_version_id`, `source_id`, or `cycle_time` range filters
- **THEN** dry-run counts, missing-quality checks, batch identity selection,
  deletion, and manifest affected-run lists SHALL all use the same filtered
  candidate set

### Requirement: Historical No-Curve Cleanup Supports Bounded Resume Evidence

The cleanup command SHALL execute destructive cleanup in bounded batches and
record enough evidence to resume or audit partial completion.

#### Scenario: Batch manifest records committed progress

- **WHEN** apply mode deletes candidate rows in multiple batches
- **THEN** the manifest SHALL include per-batch deleted row counts, duration,
  status, and a cursor or continuation hint
- **AND** the continuation hint SHALL be based on the stable row identity tuple,
  not offset pagination

#### Scenario: Timescale metadata absence is non-fatal

- **WHEN** Timescale chunk metadata is unavailable
- **THEN** dry-run manifest generation SHALL still succeed
- **AND** it SHALL record chunk distribution as unavailable while retaining time
  bucket distribution

#### Scenario: Existing production artifacts are out of scope

- **WHEN** cleanup runs in dry-run or apply mode
- **THEN** it SHALL NOT delete `hydro.river_timeseries` rows or object-store
  `/runs` artifacts
- **AND** it SHALL NOT perform schema or index maintenance such as `DROP INDEX`,
  `REINDEX`, or `VACUUM FULL`
