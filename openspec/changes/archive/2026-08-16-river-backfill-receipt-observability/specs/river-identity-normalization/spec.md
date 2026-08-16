## MODIFIED Requirements
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


The runner's stop/receipt observability SHALL additionally distinguish
lock contention from slowness and keep totals honest about unmeasured
chunks: SQLSTATE 55P03 (lock_not_available) and 40P01
(deadlock_detected) during a batch SHALL stop the run under a dedicated
stop stage `lock_contention` (no halved-range retry, remediation advice
distinct from `duration_wall`); a shortfall stop whose diagnostic counts
are both zero (`unmatched_rows == 0` and `unmappable_rows == 0`) SHALL
name the concurrent-DELETE double-snapshot signature in its reason so
operators re-check the parser re-parse window before escalating as data
corruption; and `totals.pending_rows` SHALL be documented as summing
only chunks measured in the invocation (a skipped chunk contributes
nothing, so 0 does not assert the table is sentinel-free).

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

#### Scenario: Double-zero shortfall names the concurrent-DELETE signature

- **WHEN** a batch stops with `shortfall > 0` while both
  `unmatched_rows` and `unmappable_rows` are zero
- **THEN** the stop remains fail-closed under stage `shortfall` with
  unchanged rollback and cursor-rewind behavior, and the stop reason
  directs the operator to check for a concurrent DELETE (parser
  re-parse window) between the candidate count and the UPDATE before
  treating the stop as referential rot or enum overflow

#### Scenario: Lock contention stops under its own stage, not duration_wall

- **WHEN** the batch UPDATE fails with SQLSTATE 55P03 or 40P01
- **THEN** the run stops fail-closed under stage `lock_contention`
  without a halved-range retry, the reason carries the SQLSTATE and
  advises pausing the ingest writer / waiting for an idle window (with
  the final-sweep quiescence gate noted as enforcing that pause on the
  active chunk only) rather than tuning batch size or the duration wall,
  the receipt validates against the schema, and the statement-timeout
  (57014) path keeps its existing halved-retry-then-`duration_wall`
  behavior unchanged

#### Scenario: totals.pending_rows covers only measured chunks

- **WHEN** every eligible chunk in an invocation reaches zero pending
  rows while at least one chunk was skipped as compressed or active
- **THEN** the receipt may legally report `totals.pending_rows == 0`
  with the skipped chunk's per-chunk `pending_rows` null and the
  `chunks_skipped_*` counters non-zero, and the schema documents that
  this total covers only measured chunks rather than asserting the
  whole table is sentinel-free
