# authority-table-planner-hygiene

## ADDED Requirements

### Requirement: Remove the identifier trigram index on met.met_station

The system SHALL NOT carry a trigram GIN index over the bare `station_id` column
of `met.met_station`; `met_station_id_trgm_idx` SHALL be dropped rather than
rebuilt on an expression.

`met_station_id_trgm_idx` is a trigram GIN over the bare `station_id` column.
Since pg_trgm 1.6 `gin_trgm_ops` answers `=`, so the planner may select it for
equality lookups that also carry its `active_flag = true` partial predicate, and
it does so on this table's data. Measurement over the cluster's entire counter
history shows no production consumer for it, so the expression-index convention
of ADR 0004 — which exists to keep a useful index while removing the trap — is
the wrong tool here.

#### Scenario: The index is absent after the migration

- **WHEN** the migration set has been applied in full
- **THEN** `met_station_id_trgm_idx` SHALL NOT exist on `met.met_station`, and
  neither SHALL `met_station_id_trgm_idx_invalid` or
  `met_station_id_trgm_idx_legacy`

#### Scenario: An equality lookup can no longer reach it

- **WHEN** a query filters `met.met_station` by `active_flag = true` together
  with an equality predicate on `station_id`
- **THEN** the chosen plan SHALL NOT reference `met_station_id_trgm_idx`, and
  SHALL still not reference it when sequential, index and index-only scans are
  disabled so that a bitmap scan is the only remaining option

#### Scenario: Station search returns what it returned before

- **WHEN** station search runs with a keyword, before and after the drop
- **THEN** the returned station set and `total_count` SHALL be identical, the
  query text being unchanged

#### Scenario: The drop never blocks live readers

- **WHEN** the migration runs, including when an earlier interrupted attempt left
  a renamed index behind
- **THEN** every `DROP INDEX` SHALL carry `CONCURRENTLY`, no statement SHALL take
  an ACCESS EXCLUSIVE lock on `met.met_station`, and re-running the migration
  SHALL succeed without error
