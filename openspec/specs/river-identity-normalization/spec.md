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

### Requirement: in-boundary river_timeseries readers SHALL filter by surrogate keys with field-identical external responses

Display-boundary readers of `hydro.river_timeseries` SHALL filter by the surrogate key and enum columns as the row-selection authority, and SHALL additionally retain redundant text pushdown predicates on exactly `run_id`, `river_network_version_id`, and `variable` — each conjoined (AND) with its key or enum counterpart — in every fact query whose plan can reach compressed chunks, as declared transitional aids for compressed-chunk `segmentby`/`orderby` predicate pushdown while compression settings remain text-based (user-adjudicated remedy, issue #1341 comment thread; removed together with the text-column drop in #1342, where any missed removal fails loudly because the columns are gone). These pushdown predicates are strict no-ops for key-carrying rows and MUST NOT widen results: NULL-key rows stay excluded by the key predicates. No other text column may appear as a fact predicate, with one positional exception below. The aids apply where the identity arrives as a bound literal; identity that reaches the fact table through an authority-table join stays key-joined only — text-column fact joins remain forbidden outside the sanctioned probe bodies — so such query legs carry only the aids whose identity is bound (typically `variable` alone).
Round-3 amendment (P1 EXPLAIN-gate interception, PR #1443: the set-based national legs lost the per-segment probe path and regressed 0.77s→34.7s): inside the two `hydro-national` `CROSS JOIN LATERAL` probe bodies in `services/tiles/mvt.py` — and only there — correlated text equalities on `run_id`, `river_network_version_id`, and `river_segment_id` are sanctioned as the same class of transitional pushdown aids: each is conjoined (AND) with its surrogate-key counterpart in the same probe, each is a strict no-op for key-carrying rows (all three are NOT NULL primary-key columns), and all are removed together with the text-column drop in #1342. This positionally widens the user-adjudicated three-column literal-aid set by `river_segment_id` for the lateral probe bodies only — recorded as a deviation in the PR 偏离记录 for user review, since the three-column set was a user-adjudicated remedy. Outside a lateral probe body the prohibition on text-column fact joins stands unchanged, and the shape oracle (`LATERAL_PROBE_TEXT_PUSHDOWN_COLUMNS` vs `FORBIDDEN_TEXT_FACT_COLUMNS`) enforces exactly this positional split.
This covers `services/tiles/mvt.py`,
`packages/common/display_coverage.py`, and
`apps/api/routes/hydro_display.py`. It also governs any future
identity-predicated fact query under `services/production_closure/`; that set is
empty at delivery time — the directory's `river_timeseries` references are
table-level deny-write probes, an evidence-token string, and one static plan
fixture, none of which carry an identity predicate (per-file disposition in
design.md). The requirement is: resolving caller-supplied text
identity through the four authority tables and restoring text output
via authority joins or enum-to-text casts, so that external responses
remain field-identical to the text-predicate era: JSON responses
byte-identical, MVT tiles equal as decoded feature sets, `feature_id`
concatenation byte-identical, and any ordering over identity columns
expressed on the restored text values. An unknown identity value or an
out-of-vocabulary enum literal SHALL yield the same empty result the
text predicates produced, never a SQL error. The switched read shapes
SHALL be served by an integer discovery index on `(run_key,
basin_version_key, river_network_version_key, variable_e, valid_time
DESC)` added by migration without dropping any existing text index;
text columns and text indexes remain authoritative for rollback and
for out-of-boundary readers until their separately delivered
retirement. Legacy rows whose surrogate keys remain NULL (only rows
outside the receipted backfill scope, i.e. compressed chunks pending
retention) are invisible to key-filtered reads; this exclusion is an
explicit, recorded contract with a bounded convergence deadline, not
silent data loss.

#### Scenario: Switched reads are field-identical for resolvable identities

- **WHEN** the same display request (tile, valid_times, coverage, or
  existence probe) is issued for an identity whose rows all carry
  surrogate keys, before and after the read-path switch
- **THEN** JSON responses are byte-identical, MVT tiles decode to equal
  feature sets (all properties including the `feature_id`
  concatenation, and geometry), and response ordering is unchanged

#### Scenario: Unknown or out-of-vocabulary identity degrades to empty, not error

- **WHEN** a switched query binds a `run_id` absent from
  `hydro.hydro_run` or a `variable` literal outside
  `hydro.river_variable`
- **THEN** the query returns the empty result the text predicates
  returned, and no enum-cast or other SQL error escapes to the caller

#### Scenario: NULL-key legacy rows are excluded as a recorded, converging contract

- **WHEN** rows with NULL surrogate keys exist in compressed chunks
  that the backfill runner cannot update
- **THEN** key-filtered reads exclude those rows, the exclusion scope
  (chunk ranges, row counts, retention deadline) is recorded in the
  delivery evidence, and no in-boundary reader re-admits NULL-key rows
  through text predicates: the sanctioned transitional pushdown
  predicates are conjunctive and can only narrow, never widen, the
  key-filtered result

#### Scenario: Transitional text pushdown predicates are bounded to the sanctioned set and paired with keys

- **WHEN** an in-boundary fact query contains a text identity predicate
- **THEN** that predicate is on `run_id`, `river_network_version_id`,
  or `variable` only, appears in the same conjunction as its surrogate
  key or enum counterpart, and no text predicate on
  `basin_version_id` or `river_segment_id` (nor any text-column join
  into the fact table) exists in any in-boundary read shape — except
  inside the two `hydro-national` `CROSS JOIN LATERAL` probe bodies,
  where the round-3 amendment above additionally sanctions correlated
  text equalities on `run_id`, `river_network_version_id`, and
  `river_segment_id`, each key-paired, removed with #1342; no `ts.`
  fact reference may appear outside those probe bodies in the national
  legs

#### Scenario: Switched shapes are served by the integer index without text-read regression

- **WHEN** the switched query shapes run on the production-scale
  database after the integer discovery index is applied
- **THEN** `EXPLAIN (ANALYZE, BUFFERS)` shows them planned on the
  integer index with no sequential scan of `hydro.river_timeseries`
  and latency no worse than the text-index baseline, while retained
  text indexes keep serving out-of-boundary text readers unchanged;
  shape carve-out (round 3): the two `hydro-national` lateral probe
  legs instead plan as per-segment parameterized probes on the text
  primary key (uncompressed chunks) and the compressed `segmentby`
  index (compressed chunks) — the integer index remains the planned
  path for every other switched shape, and #1342 owns the post-cutover
  index set that replaces the text plans for these two legs

#### Scenario: Compressed-chunk portions keep predicate pushdown via the transitional text predicates

- **WHEN** a switched query shape whose plan reaches a compressed chunk
  (text-based `segmentby`/`orderby` settings still in force) runs with
  the transitional pushdown predicates present
- **THEN** the compressed-chunk portion of the plan shows an index or
  filter condition on the compression-internal relation driven by the
  text `segmentby`/`orderby` columns, not a full-decompression
  sequential scan over all batches

