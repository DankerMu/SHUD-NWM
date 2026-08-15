# display-coverage-freshness Delta

## ADDED Requirements

### Requirement: Publish is a status-only transition for coverage freshness

Publishing a parsed run for display SHALL NOT advance the run's `updated_at`, so that coverage refreshed during ingest remains fresh across the publish transition; `updated_at` advances only on data mutations (registration, parse completion).

#### Scenario: No false staleness after publish

- **WHEN** a run whose display coverage was refreshed during ingest is subsequently published
- **THEN** the coverage staleness predicate (`refreshed_at < updated_at`) does not mark that run stale
- **AND** the coverage backstop reports zero stale runs attributable to the publish transition alone.

#### Scenario: Display cache revision still rotates on publish

- **WHEN** a run transitions from `parsed` to `published`
- **THEN** the MVT tile revision digest changes because its basis includes the run `status`
- **AND** removing the publish-time `updated_at` bump does not suppress tile cache rotation.

### Requirement: Autopipe tick phases expose per-phase durations

The autopipe cron tick SHALL log a distinguishable elapsed-time line for each of its phases (ingest, coverage backstop, MVT prewarm), in addition to the whole-tick markers.

#### Scenario: Phase durations in cron log

- **WHEN** one autopipe tick completes
- **THEN** the cron log contains one elapsed-seconds line per executed phase, each naming its phase
- **AND** the whole-tick START/END markers remain present.
