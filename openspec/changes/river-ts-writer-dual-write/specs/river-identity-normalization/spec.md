# Delta: river-identity-normalization（issue #1340 写路径双写）

## ADDED Requirements

### Requirement: river_timeseries writers SHALL dual-write surrogate identity columns atomically with the text columns

Writers SHALL populate surrogate identity in the same statement as text:
every production or seed writer that INSERTs into `hydro.river_timeseries`
populates the seven normalized columns (`run_key`,
`river_network_version_key`, `basin_version_key`, `river_segment_key`,
`variable_e`, `unit_e`, `quality_flag_e`) in the same INSERT statement
that writes the legacy text columns, leaving the text-column write
byte-identical to the pre-change behavior. Surrogate keys SHALL be
resolved by read-only SELECTs against the four authority tables — the
dual-write path never creates authority rows (pre-existing seed
authority inserts are out of scope); for the production parser this
means extending its existing context/segment load queries with zero
additional round-trips. An unresolvable identity value
SHALL fail the whole batch closed with a structured error — a NULL
surrogate on a newly written row is never a legal outcome. Enum columns
SHALL receive the same in-process text value as their text counterparts,
coerced server-side by the enum column type, so text↔enum divergence is
unrepresentable in the writer and out-of-vocabulary values fail closed.
Conflict-update branches SHALL
mirror the text columns they refresh: every text column in the
`ON CONFLICT ... DO UPDATE SET` list has its surrogate counterpart set
from `EXCLUDED`, and identity (conflict-target) columns are re-set on
neither side.

#### Scenario: New rows carry a complete, consistent surrogate identity

- **WHEN** a parse run writes rows through the dual-write path into a
  database carrying migration 000050
- **THEN** every newly written row has all seven normalized columns
  non-NULL, the read-only verify function reports zero equality-audit
  divergence for those rows, and the legacy text columns are populated
  exactly as before the change

#### Scenario: Conflict updates cannot re-introduce text↔surrogate drift

- **WHEN** the writer's INSERT statement is replayed directly against an
  existing row whose identity matches but whose refreshable text columns
  (`basin_version_id`, `unit`, `quality_flag`) have drifted, so the
  `ON CONFLICT DO UPDATE` branch fires (the production DELETE-replace
  path removes conflicting rows first — this branch is the safety net
  for concurrent or replayed writes, and is exercised by replaying the
  statement without the preceding DELETE)
- **THEN** the surviving row's `basin_version_key`, `unit_e`, and
  `quality_flag_e` reflect the same update as their text counterparts,
  and the identity columns (text and surrogate) are unchanged

#### Scenario: Unresolvable identity or out-of-vocabulary value fails the batch closed

- **WHEN** a row references an identity value absent from its authority
  table, or carries a variable/unit/quality-flag literal outside the
  corresponding enum's value set
- **THEN** the batch write raises a structured error and no rows from
  that batch are persisted — the writer never falls back to writing
  NULL surrogates on new rows

#### Scenario: Dual-write coexists with the #1339 backfill lane

- **WHEN** dual-written rows and legacy text-only rows coexist in the
  table and the identity backfill runner executes
- **THEN** only legacy rows (NULL sentinel) are counted as backfill
  candidates, dual-written rows are not re-updated, and rolling the
  writer back to the pre-change code merely produces new sentinel rows
  that the existing backfill lane converges later — no data loss in
  either direction
