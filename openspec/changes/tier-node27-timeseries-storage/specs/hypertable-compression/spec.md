# hypertable-compression Specification (delta)

## ADDED Requirements

### Requirement: Compression settings cover primary keys

Both detail hypertables SHALL have TimescaleDB compression enabled via
migration with segmentby/orderby that jointly cover every primary-key
column: `hydro.river_timeseries` segmentby
`run_id, river_network_version_id, river_segment_id` orderby
`variable, valid_time`; `met.forcing_station_timeseries` segmentby
`forcing_version_id, station_id` orderby `variable, valid_time`.

#### Scenario: Settings visible after migration

- **WHEN** the compression migration has been applied
- **THEN** `timescaledb_information.hypertables` MUST show
  `compression_enabled = true` for both tables
- **AND** `timescaledb_information.compression_settings` MUST list, for both
  hypertables, exactly the configured segmentby columns (rows with
  `segmentby_column_index` set) and orderby columns (rows with
  `orderby_column_index` set), since on TimescaleDB 2.10 the
  `hypertables` view does not expose segmentby/orderby settings

### Requirement: Terminal-chunk-only compression

The compression runner SHALL compress only chunks whose `range_end` is older
than a configurable lag (default 7 days) and SHALL never compress the active
chunk.

#### Scenario: Recent chunk is skipped

- **WHEN** a chunk's `range_end` is within the configured lag of now
- **THEN** the runner MUST NOT compress it

#### Scenario: Terminal chunks are compressed with receipts

- **WHEN** the runner compresses eligible chunks
- **THEN** the receipt MUST record per-chunk before/after bytes and the
  per-table totals

### Requirement: Scheduled, bounded, governance-visible compression operation

Steady-state compression SHALL run from a node-27 user-level systemd timer
following the existing governance-family patterns: the runner SHALL default
to dry-run, require an explicit enforce flag to compress, hold a flock
(single instance), compress at most a configurable number of chunks per
tick, and emit a JSON receipt per run; its service/timer units SHALL be
registered in the resource-governance audit unit list.

#### Scenario: Governance audit reports compression units

- **WHEN** the node-27 resource-governance audit runs
- **THEN** its receipt MUST include the compression service/timer states

#### Scenario: Eligible chunks exceed the per-tick bound

- **WHEN** more terminal chunks are eligible than the per-tick maximum
- **THEN** only the maximum count MUST be compressed and the receipt MUST
  list the deferred remainder

#### Scenario: Dry-run tick performs no compression

- **WHEN** a scheduled tick runs without the enforce flag or while the flock
  is already held
- **THEN** it MUST compress nothing and emit a receipt recording the
  candidate list (dry-run) or the lock skip

### Requirement: Reingest fails closed on compressed chunks

Ingest or reingest writes targeting a compressed chunk SHALL abort with an
explicit error that names the chunk and references the documented
`decompress_chunk` procedure; silent skips or partial writes are forbidden.
This guard applies to every write path into the production hypertables; the
archive rebuild drill never triggers it because the drill writes only its
isolated staging schema (see `archive-rebuild-drill`), an exemption by
isolation, not a bypass of the guard.

Scope carve-out — batch-time-range only: The guard's semantic scope is the
batch's `[min(valid_time), max(valid_time)]` window. The identity-scoped
older-history residual — where an identity-scoped DELETE hits older
compressed chunks OUTSIDE the batch valid_time window because the identity
has historical data older than the batch — is OUT OF SCOPE for this
requirement; operators handle it via the runbook §4.2 residual procedure.
The guard passing on this case is not a bypass: TimescaleDB's own raw error
still blocks the write, and the runbook covers the decompress + retry
sequence.

#### Scenario: Reingest targets a compressed window

- **WHEN** a reingest operation would write rows into a compressed chunk
- **THEN** the operation MUST abort with the fail-closed error before any
  row mutation
- **AND** the error MUST reference the runbook decompress procedure

#### Non-Scenario: Older-history residual outside the batch window

- **WHEN** the batch's `valid_time` window is entirely outside any compressed
  chunk, but the identity-scoped DELETE would hit older compressed chunks
  outside that window
- **THEN** the guard MUST NOT block; TimescaleDB's own raw error on the
  DELETE remains the enforcement layer; operators recover via the runbook
  §4.2 residual procedure

### Requirement: Initial compression produces a size receipt

The one-time initial compression of existing terminal chunks SHALL produce a
committed receipt recording per-table total bytes before and after, and
representative curve/MVT query timings before and after.

#### Scenario: Initial run receipt

- **WHEN** the initial terminal-chunk compression completes on node-27
- **THEN** a receipt with before/after totals for both hypertables and the
  representative query timings MUST be committed to the repository
