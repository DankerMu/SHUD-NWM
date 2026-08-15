# river-identity-normalization Specification

## Purpose
TBD - created by archiving change river-identity-normalization-backfill. Update Purpose after archive.
## Requirements
### Requirement: river_timeseries identity columns SHALL have integer surrogate-key targets with an idempotent, bounded, receipted backfill

The schema SHALL provide integer surrogate keys on the four existing
authority tables (`hydro.hydro_run`, `core.basin_version`,
`core.river_network_version`, `core.river_segment`), native enum types
for `variable`, `unit`, and `quality_flag`, and nullable normalized
columns on `hydro.river_timeseries` added without a table rewrite.
Backfill of existing rows SHALL run only through a bounded, receipted
runner that is dry-run by default, batches by ctid block ranges, is
resumable via a NULL-sentinel predicate plus a persisted block cursor,
enforces a per-batch transaction-duration wall, fails closed (with
distinguishable receipt counters) on authority-unmatched or
enum-unmappable values, and never issues DML against compressed chunks
or the active (currently ingested) chunk. The production compression
settings and primary key SHALL remain unchanged by this change;
switching both is delivered as a read-only verify function plus a
fail-closed cutover function that the migration chain never invokes.

#### Scenario: Migration replays idempotently without rewriting the fact table

- **WHEN** the migration chain through 000050 is applied twice to an
  empty database
- **THEN** both passes succeed, the four surrogate-key columns, three
  enum types, and seven nullable fact columns exist exactly once, and
  `pg_attribute.atthasmissing` is false for all seven new fact columns
  (no stored default, no rewrite)

#### Scenario: Backfill is re-entrant and bounded

- **WHEN** the backfill runner is interrupted after some batches and
  re-invoked
- **THEN** it resumes without re-updating rows whose sentinel key is
  already set (block cursor loss degrades to a full rescan with
  identical results), a second complete pass reports zero changed rows,
  every batch runs inside its own transaction bounded by the configured
  duration wall with one halved-range retry, and each invocation emits
  a schema-validated receipt

#### Scenario: Unresolvable values, compressed chunks, and the active chunk fail safe

- **WHEN** the runner detects a per-batch shortfall between sentinel
  candidates and updated rows (authority-unmatched or enum-unmappable
  values), or encounters a chunk reported compressed by the shared
  write-guard's chunk assertion, or the active chunk
- **THEN** the shortfall stops the run fail-closed with distinguishable
  receipt counters (never silently left as progress), compressed and
  active chunks receive no UPDATE and are listed in the receipt as
  skipped, and the documented recovery paths are the existing
  decompression-replay/compression runners (compressed) and either a
  later catch-up round once the chunk is terminal or an explicit
  final-sweep invocation that first asserts ingest is quiescent
  (active)

#### Scenario: Cutover is a fail-closed single-transaction window operation, never auto-applied

- **WHEN** `hydro.cutover_river_identity_normalization()` is invoked
  with any compressed chunk present, or with any NULL remaining in the
  seven normalized columns
- **THEN** it raises an error and changes nothing (a compressed chunk
  fails the explicit precondition; a NULL fails the in-transaction
  VALIDATE CONSTRAINT step, rolling everything back); only inside a
  maintenance window — ingest paused, final sweep done, read-only
  verify counts at zero, all chunks decompressed — does it execute the
  measured working sequence in one transaction: disable compression,
  drop the text foreign key (measured TimescaleDB 2.10 rule: foreign
  key columns must be covered by segmentby∪orderby, so the text FK
  cannot survive integer segmentby), validate NOT NULL via check
  constraints then set the seven columns NOT NULL scan-free, replace
  the primary key with the integer/enum form (in-window index build),
  and re-enable compression with segmentby/orderby on the normalized
  columns (TimescaleDB 2.10 requires segmentby∪orderby to cover the
  primary-key columns, so the two switches are inseparable), after
  which a compress/decompress round-trip preserves row data; the
  migration chain itself never calls the verify or cutover functions,
  keeping CI and production schemas convergent

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

