## ADDED Requirements

### Requirement: The expand migration SHALL rename in place and create the narrow table without decompressing or rebuilding anything

The river expand migration SHALL, in one transaction: rename `hydro.river_timeseries` to `hydro.river_timeseries_legacy`; create the narrow `hydro.river_timeseries` in the order `CREATE TABLE` (primary key and both foreign keys inline) → `create_hypertable(..., chunk_time_interval => interval '1 day', create_default_indexes => false)` → the two secondary indexes → `ALTER TABLE ... SET (timescaledb.compress, compress_segmentby, compress_orderby)` → `ALTER TABLE ... OWNER TO` the ingest write role; add `hydro.hydro_run.timeseries_store` with default `narrow` and backfill `legacy` by the parse-fact predicate `parsed_at IS NOT NULL OR status IN ('parsed','published')` (legacy remnants outside it are the accepted transitional state of `timeseries-narrow-store`; no row-level probe of the legacy table). It MUST NOT decompress any chunk, MUST NOT create an index on the legacy table, MUST NOT alter the legacy table's compression settings, MUST NOT re-run migration `000047`, and MUST be idempotent on re-run.

#### Scenario: Idempotent re-run
- **WHEN** the expand migration is applied twice
- **THEN** the second application changes nothing and exits successfully

#### Scenario: Legacy table untouched and statements are metadata-only
- **WHEN** the expand migration is applied on a database whose river hypertable has compressed chunks
- **THEN** every chunk keeps its compression state, the legacy table keeps its primary key, indexes, compression settings and owner, and the node-27 receipt records per-statement wall time with no statement exceeding a metadata operation

#### Scenario: Compression setting is the last schema DDL
- **WHEN** the migration text is read
- **THEN** `timescaledb.compress` is set after every constraint and index statement on the narrow table, and a test pins that order

### Requirement: Lifecycle lanes and catalog-pinning tools SHALL cover the legacy table until it is dropped, without a second code change

The compression runner, the retention runner, the compression supervisor's current-state validation, the capture tool's hypertable keys, the live-evidence replay validator's catalog check, the autopipeline statistics guard and the resource-governance collection SHALL derive their hypertable set as each canonical table plus its `_legacy` sibling when that sibling exists in `timescaledb_information.hypertables`; per-table expectations SHALL be asserted per table (key-shaped settings on the canonical table, text-shaped settings on the legacy table). Both tables SHALL be compressed under the same lag and dropped under the same retention window. The compression receipt schema SHALL accept `_legacy` keys in `per_table_totals` as optional, and the retention receipt SHALL carry `legacy_chunks` per legacy table. When the legacy table no longer exists the tools MUST proceed with the canonical table only. A canonical table without a `_legacy` sibling SHALL take its expected compression shape from a per-table no-sibling default owned by the shared discovery helper (text-shaped for both tables until each contract); the river contract migration (I9) and the forcing contract migration (I14) SHALL flip that table's default to key-shaped in the same PR as the DROP and deploy it before the DROP runs, so the expectation never reverts to text-shaped once the sibling is gone.

#### Scenario: Both tables in one tick
- **WHEN** a compression tick runs while `hydro.river_timeseries_legacy` exists with an eligible chunk and `hydro.river_timeseries` has an eligible one-day chunk
- **THEN** the receipt lists chunks from both hypertables under the per-tick bound, validates against the schema, and the supervisor's validation accepts the mixed catalog

#### Scenario: Legacy gone
- **WHEN** the legacy table has been dropped
- **THEN** a tick, the supervisor validation and the statistics guard run cleanly with no reference to the legacy name, and the retention receipt omits `legacy_chunks`

### Requirement: Rollback before the contract SHALL be an executable reverse sequence with a recorded intermediate state

While the legacy table exists, the runbook SHALL provide the reverse sequence: stop timers and the API/parser units → `ALTER TABLE hydro.river_timeseries RENAME TO hydro.river_timeseries_narrow_rollback` → `ALTER TABLE hydro.river_timeseries_legacy RENAME TO hydro.river_timeseries` → deploy the pre-change code → `UPDATE hydro.hydro_run SET timeseries_store = 'legacy'` → start. The state "store is `legacy` while narrow rows for that run still exist in the renamed narrow table" is an allowed rollback state: those rows are invisible to every read path and are removed by dropping the renamed narrow table before any new expand attempt.

#### Scenario: Rollback rehearsal on a throwaway cluster
- **WHEN** the reverse sequence is executed after a narrow run was parsed
- **THEN** the legacy run is readable and re-parseable by the pre-change code, the previously narrow run is re-parsed into the (again canonical) text table, and the renamed narrow table drops cleanly

### Requirement: The contract migration SHALL refuse while the legacy table holds any chunk, and code SHALL stop reading the routing column before it is dropped

The river contract migration SHALL count the legacy table's chunks first and MUST raise, changing nothing, when the count is non-zero. When zero it SHALL drop the legacy table, drop `hydro.cutover_river_identity_normalization()` and `hydro.verify_river_identity_normalization()`, and drop `hydro.hydro_run.timeseries_store`. The contract window SHALL deploy the code that no longer references `timeseries_store` before applying the migration.

#### Scenario: Non-empty legacy refused
- **WHEN** the contract migration runs while one legacy chunk remains
- **THEN** it raises naming the chunk count and the catalog is unchanged

#### Scenario: Clean contract
- **WHEN** the contract migration runs after retention has emptied the legacy table and the routing-free code is deployed
- **THEN** the legacy table, both functions and the routing column are gone and `tests/test_migrations.py` pins the final three-index shape

### Requirement: Contract SHALL remove every transitional aid and the legacy read variant

After the contract batch, no file in the repository SHALL contain the marker `transitional compressed-chunk pushdown aid, remove with #1342`; the renderer SHALL accept only the narrow store; `scripts/node27_river_identity_backfill.py` and its tests SHALL be deleted; `tests/test_river_ts_dual_write_integration.py` SHALL be deleted or re-pinned as a narrow-write integration; the cleanup oracles SHALL assert that no fact-table SQL references any text identity column.

#### Scenario: Marker census is zero
- **WHEN** `grep -rn "remove with #1342" --include=*.py --include=*.sql .` runs after the contract batch
- **THEN** it returns nothing

#### Scenario: Oracle rejects a reintroduced text predicate
- **WHEN** a registered reader adds `rt.run_id = %s` to its template
- **THEN** the cleanup oracle fails naming the call site

### Requirement: Rollout and contract SHALL each be gated by node-27 live receipts

The river rollout SHALL not be declared complete without a node-27 receipt recording: expand migration wall time per statement; the first narrow chunk's size after one full cycle; the per-segment EXPLAIN gate and the probe before/after plans (`timeseries-narrow-store`); registry counts (active/runnable/selected/excluded); one compression tick and one retention tick covering both tables; the governance receipt with the working-set fields; `/` clicks on SHJ-NJ, one medium and one small network with both source curves rendered and identities not crossed (screenshot evidence); the display read-only boundary deny-write receipt; `/ops` reachable. The contract SHALL not run before fourteen consecutive daily receipts show the narrow route healthy and the retention receipt shows `legacy_chunks["hydro.river_timeseries_legacy"] = 0`; the fourteen-day wait is the entry gate of the contract issue, not a task of the rollout issue.

#### Scenario: Contract precondition
- **WHEN** an operator prepares the contract window
- **THEN** the runbook checklist requires the archived fourteen daily receipts and the retention receipt with `legacy_chunks["hydro.river_timeseries_legacy"] = 0` (the per-table mapping added by I6) before the migration may be applied

#### Scenario: Maintenance window order
- **WHEN** the expand window runs
- **THEN** the order is: timers stopped → API and parser units stopped → `git pull --ff-only` → `migrate.py` → units started → timers started, and the receipt records that no request was served between the rename and the restart
