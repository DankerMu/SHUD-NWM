-- Change `river-identity-normalization-backfill` (issue #1339). Adds the
-- integer surrogate-key TARGETS for `hydro.river_timeseries`'s repeated text
-- identity columns. This migration changes SHAPE ONLY: it backfills nothing,
-- it switches no primary key, it touches no compression setting, and it calls
-- neither of the two functions it defines. Backfill is
-- `scripts/node27_river_identity_backfill.py`; the pkey/segmentby switch is an
-- operator-invoked maintenance-window call of
-- `hydro.cutover_river_identity_normalization()` per runbook
-- docs/runbooks/tier-node27-timeseries-storage.md section 4.6.
--
-- WHY the migration body calls neither function: an empty database (CI,
-- hermetic test DBs, a future node) would auto-apply the cutover and end up
-- with a primary key and compression settings that production does not have.
-- Divergent CI/production schemas destroy the oracle. The functions ship here
-- so they are version-controlled and replayable; invoking them is an operator
-- act, not a migration act.
--
-- ---------------------------------------------------------------------------
-- LOCK COST (measured, node-27 throwaway `nhms_1339_probe`, 2026-08-15,
-- PG 15.2 / TimescaleDB 2.10.2; full log in
-- openspec/changes/river-identity-normalization-backfill/probe-1339-throwaway.md,
-- house precedent for recording measured cost in the header: 000049:34)
-- ---------------------------------------------------------------------------
--
-- `INTEGER GENERATED ALWAYS AS IDENTITY` cannot use the PG 11+ fast-default
-- path: the column default is the volatile `nextval()`, so every existing row
-- must be materialised. Adding it is a FULL TABLE REWRITE under ACCESS
-- EXCLUSIVE. Measured on replicas carrying the live row counts and the
-- complete live index set:
--
--   core.river_segment         209,126 rows, 106 MB replica -> 5.348 s
--                              209,126 rows, 228 MB replica -> 6.618 s
--                              live is 355 MB total (278 MB heap+toast,
--                              77 MB across 7 indexes: pkey, GIST(geom),
--                              3x GIN trgm, 2x btree) -> budget ~10 s AEL.
--                              Cost is dominated by rebuilding those 7
--                              indexes, not by the row count.
--   hydro.hydro_run            3,609 rows   -> 61 ms
--   core.basin_version         20 rows      -> 46 ms
--   core.river_network_version 20 rows      -> 47 ms
--
-- `core.river_segment` is on the MVT production read path (six JOINs in
-- services/tiles/mvt.py), so a queued ACCESS EXCLUSIVE request would stall
-- every reader behind it. Each authority ALTER therefore runs under
-- `SET LOCAL lock_timeout` (measured: a competing reader makes the ALTER fail
-- with `canceling statement due to lock timeout` at exactly the configured
-- wait, rather than queueing). Note `lock_timeout` bounds only the WAIT for
-- the lock -- not the ~10 s rewrite that follows once the lock is held. Apply
-- this migration at low peak; on timeout, re-run (each block is idempotent).
--
-- Re-running an already-applied `ADD COLUMN IF NOT EXISTS ... IDENTITY` is a
-- 0.563 ms catalog no-op (measured) -- no second rewrite.
--
-- ---------------------------------------------------------------------------
-- WHY THE SEVEN FACT COLUMNS CARRY NO DEFAULT
-- ---------------------------------------------------------------------------
--
-- Measured, NOT assumed: on TimescaleDB 2.10.2 a nullable no-default
-- `ADD COLUMN` on `hydro.river_timeseries` is metadata-only at 0.8-1.1 ms per
-- column even with a compressed chunk present, and leaves
-- `pg_attribute.atthasmissing = false` on the parent and on every chunk.
--
-- The reason for omitting a default on `quality_flag_e` (whose text
-- counterpart `quality_flag` carries `DEFAULT 'ok'`) is NOT that a default
-- would rewrite the table -- the control measurement disproves that: adding a
-- constant-default column on the same compressed-chunk hypertable also took
-- 1.532 ms via the PG 11+ fast-default path. The two real reasons are:
--
--   1. Sentinel purity. The backfill runner's re-entrancy and its
--      candidate-count-vs-rowcount shortfall detection both depend on
--      "not yet backfilled" being expressible as IS NULL. A default would
--      pre-fill every existing row with a value the runner cannot distinguish
--      from work it actually did, and the cutover's VALIDATE gate would then
--      pass on rows that were never resolved against the authority tables.
--   2. `atthasmissing = false` stays a mechanically checkable pin of the
--      no-default choice (it is `true` for the fast-default variant), which is
--      what the migration-replay integration test asserts.
--
-- No foreign keys and no indexes are created on the new columns. The FK
-- omission is a MEASURED HARD CONSTRAINT, not YAGNI: TimescaleDB 2.10.2
-- requires every foreign-key column to be covered by
-- `compress_segmentby` U `compress_orderby`, and `basin_version_key` is not in
-- the target segmentby, so such an FK would make the cutover's compression
-- ALTER fail. Indexes belong to the follow-up switch issue.
--
-- Idempotency: every statement below is guarded (`IF NOT EXISTS`, catalog
-- probe, or `duplicate_object` handler). Under the autocommit apply lanes
-- (packages/common/migrate.py and the psql replay lane) each top-level
-- statement commits on its own, so a partial apply is completed by a re-run.
-- No explicit transaction wrapper, matching the repo house style
-- (000045_hydro_run_type_hindcast.sql, 000047_hypertable_compression_settings.sql).

-- ---------------------------------------------------------------------------
-- 1. Native enum types (house style: 000003_enums.sql)
-- ---------------------------------------------------------------------------
--
-- Value sets are the union of three enumerated sources. Every value is
-- annotated with the source that justifies it; nothing speculative is welded
-- into a production type, because adding an enum value later is cheap
-- (`ALTER TYPE ... ADD VALUE`) while removing one is not.
--
-- Sources:
--   P = workers/output_parser/parser.py write literals -- the only production
--       writer of hydro.river_timeseries.
--         VARIABLE_Q_DOWN = "q_down"      (parser.py:25)
--         UNIT_M3S        = "m3/s"        (parser.py:26)
--         quality_flag default "ok"       (parser.py:109)
--         quality_flag "qc_warning"       (parser.py:190, QC-failed batches)
--   S = db/seeds/seed_demo.py -- demo/dev seeding, wider than production.
--         RIVER_VARIABLES = ("q_down", "y_stage")   (seed_demo.py:87)
--         unit "m3/s" for q_down, "m" otherwise     (seed_demo.py:746)
--         quality_flag "ok"                         (seed_demo.py:584,766)
--   L = node-27 live distinct, read-only from pg_stats.most_common_vals
--       across all six hydro.river_timeseries chunks, 2026-08-15. n_distinct
--       was 1 / 1 / 2 on every chunk, so the MCV list is the COMPLETE live
--       value set, not a sample: variable {q_down}, unit {m3/s},
--       quality_flag {ok, qc_warning}.
--
-- Deliberately excluded: the `'m3 s-1'` literal that appeared only in
-- tests/test_tile_publisher.py. It is a test-local orphan matching no writer
-- and no live row; that test was corrected to `'m3/s'` in the same change
-- rather than widening a production type to accommodate it.

DO $$
BEGIN
  CREATE TYPE hydro.river_variable AS ENUM (
    'q_down',   -- P (parser.py:25), S (seed_demo.py:87), L
    'y_stage'   -- S (seed_demo.py:87); no production writer yet
  );
EXCEPTION
  WHEN duplicate_object THEN null;
END $$;

DO $$
BEGIN
  CREATE TYPE hydro.river_unit AS ENUM (
    'm3/s',   -- P (parser.py:26), S (seed_demo.py:746), L
    'm'       -- S (seed_demo.py:746), the y_stage unit
  );
EXCEPTION
  WHEN duplicate_object THEN null;
END $$;

DO $$
BEGIN
  CREATE TYPE hydro.river_quality_flag AS ENUM (
    'ok',          -- P (parser.py:109), S (seed_demo.py:584,766), L
    'qc_warning'   -- P (parser.py:190), L
  );
EXCEPTION
  WHEN duplicate_object THEN null;
END $$;

-- ---------------------------------------------------------------------------
-- 2. Authority-table surrogate keys (four separate blocks: disjoint tables,
--    order-independent, individually re-runnable)
-- ---------------------------------------------------------------------------
--
-- No separate dimension tables are created. The four authority tables already
-- exist and already carry exactly the right identity grain -- in particular
-- core.river_segment's natural key is the PAIR
-- (river_segment_id, river_network_version_id) (000004_core.sql:33-42), which
-- is also the grain of the fact table's existing foreign key
-- (000006_hydro.sql:57-58). Keying off the authority row therefore cannot
-- silently merge same-named segments across network versions, and it removes
-- the seeding phase entirely (no DISTINCT scan of a 249 GB fact table is
-- needed to populate a dimension that already exists).
--
-- IDENTITY rather than the repo's BIGSERIAL precedent: IDENTITY is the SQL
-- standard form and `GENERATED ALWAYS` rejects manual inserts into the key
-- column, which is what we want for a surrogate key nothing should hand-set.
-- IDENTITY implies NOT NULL, so it is not restated.

DO $$
BEGIN
  SET LOCAL lock_timeout = '2s';
  ALTER TABLE hydro.hydro_run
    ADD COLUMN IF NOT EXISTS run_key INTEGER GENERATED ALWAYS AS IDENTITY UNIQUE;
END $$;

DO $$
BEGIN
  SET LOCAL lock_timeout = '2s';
  ALTER TABLE core.basin_version
    ADD COLUMN IF NOT EXISTS basin_version_key INTEGER GENERATED ALWAYS AS IDENTITY UNIQUE;
END $$;

DO $$
BEGIN
  SET LOCAL lock_timeout = '2s';
  ALTER TABLE core.river_network_version
    ADD COLUMN IF NOT EXISTS river_network_version_key INTEGER GENERATED ALWAYS AS IDENTITY UNIQUE;
END $$;

-- The expensive one: ~10 s ACCESS EXCLUSIVE on live (see header). Low peak.
DO $$
BEGIN
  SET LOCAL lock_timeout = '2s';
  ALTER TABLE core.river_segment
    ADD COLUMN IF NOT EXISTS river_segment_key INTEGER GENERATED ALWAYS AS IDENTITY UNIQUE;
END $$;

-- ---------------------------------------------------------------------------
-- 3. Fact-table normalized columns: seven nullable, no default, no FK, no index
-- ---------------------------------------------------------------------------
--
-- The lock_timeout guard applies here too. Each statement is ~1 ms of work,
-- but it still has to ACQUIRE ACCESS EXCLUSIVE on the busiest table in the
-- database; without the guard a request queued behind one long-running reader
-- would stall ingest and every display read behind it. Failing fast and
-- re-running is strictly better than that.

DO $$
BEGIN
  SET LOCAL lock_timeout = '2s';
  ALTER TABLE hydro.river_timeseries
    ADD COLUMN IF NOT EXISTS run_key INTEGER,
    ADD COLUMN IF NOT EXISTS river_network_version_key INTEGER,
    ADD COLUMN IF NOT EXISTS basin_version_key INTEGER,
    ADD COLUMN IF NOT EXISTS river_segment_key INTEGER,
    ADD COLUMN IF NOT EXISTS variable_e hydro.river_variable,
    ADD COLUMN IF NOT EXISTS unit_e hydro.river_unit,
    ADD COLUMN IF NOT EXISTS quality_flag_e hydro.river_quality_flag;
END $$;

-- ---------------------------------------------------------------------------
-- 4. Stage one of the switch: read-only verification (never called from here)
-- ---------------------------------------------------------------------------
--
-- Runs outside any maintenance window. It is ONE full scan of a 249 GB table
-- and takes no locks beyond ACCESS SHARE, which is the entire point: the three
-- families of counts an operator needs before committing to a window are
-- produced while the system is still serving traffic.
--
-- "One" is load-bearing. The obvious shape -- eight independent
-- `(SELECT count(*) ... WHERE <col> IS NULL)` subqueries -- is eight separate
-- heap scans of 249 GB, and a correlated scalar subquery for the
-- basin_version leg would run ~460M times on top of that. Aggregate FILTERs
-- over a single scan, plus one LEFT JOIN against a primary key, produce the
-- identical numbers for a fraction of the I/O.
--
-- The equality-audit count uses the same predicate as the backfill runner's
-- receipt counter. It detects rows whose text columns drifted away from their
-- already-backfilled surrogate columns -- which the ingest writer's
-- `ON CONFLICT DO UPDATE` branch (parser.py) can cause, since it refreshes
-- basin_version_id / unit / quality_flag but knows nothing about the `_key` /
-- `_e` columns. A non-zero value is not corruption; it means a re-sweep is
-- required before the window.

CREATE OR REPLACE FUNCTION hydro.verify_river_identity_normalization()
RETURNS TABLE (
  rows_total bigint,
  null_run_key bigint,
  null_river_network_version_key bigint,
  null_basin_version_key bigint,
  null_river_segment_key bigint,
  null_variable_e bigint,
  null_unit_e bigint,
  null_quality_flag_e bigint,
  equality_audit_divergent bigint,
  compressed_chunk_count bigint
)
LANGUAGE sql
STABLE
AS $verify$
  -- The join is written with parenthesised ON-conditions on purpose: the
  -- migration lint in tests/test_migrations.py reads `ON <schema>.<table>` as
  -- an index/table reference, and `ON (a.b = c.d)` keeps it out of that
  -- pattern without changing the plan.
  --
  -- basin_version_id is core.basin_version's PRIMARY KEY, so the LEFT JOIN
  -- matches at most one row and is exactly equivalent to the correlated
  -- scalar subquery it replaces: an unmatched fact row yields
  -- bv.basin_version_key IS NULL, which IS DISTINCT FROM treats the same way.
  SELECT
    count(*),
    count(*) FILTER (WHERE t.run_key IS NULL),
    count(*) FILTER (WHERE t.river_network_version_key IS NULL),
    count(*) FILTER (WHERE t.basin_version_key IS NULL),
    count(*) FILTER (WHERE t.river_segment_key IS NULL),
    count(*) FILTER (WHERE t.variable_e IS NULL),
    count(*) FILTER (WHERE t.unit_e IS NULL),
    count(*) FILTER (WHERE t.quality_flag_e IS NULL),
    count(*) FILTER (
      WHERE t.run_key IS NOT NULL
        AND (
             t.variable_e::text IS DISTINCT FROM t.variable
          OR t.unit_e::text IS DISTINCT FROM t.unit
          OR t.quality_flag_e::text IS DISTINCT FROM t.quality_flag
          OR t.basin_version_key IS DISTINCT FROM bv.basin_version_key
        )
    ),
    (SELECT count(*)
       FROM timescaledb_information.chunks AS c
       WHERE c.hypertable_schema = 'hydro'
         AND c.hypertable_name = 'river_timeseries'
         AND c.is_compressed)
  FROM hydro.river_timeseries AS t
  LEFT JOIN core.basin_version AS bv
    ON (bv.basin_version_id = t.basin_version_id);
$verify$;

COMMENT ON FUNCTION hydro.verify_river_identity_normalization() IS
  'Read-only pre-cutover gate for issue #1339. All three families of counts '
  '(seven NULL counts, equality-audit divergence, compressed chunks) must be '
  'zero before hydro.cutover_river_identity_normalization() is invoked. One '
  'full scan, ACCESS SHARE only, safe to run against a live serving database.';

-- ---------------------------------------------------------------------------
-- 5. Stage two of the switch: the cutover (never called from here)
-- ---------------------------------------------------------------------------
--
-- One transaction. Evidence, separated by source because the two are not
-- interchangeable:
--
--   * probe log step d-6 (node-27 throwaway, statement-by-statement in
--     autocommit) measured that disabling compression, dropping the text
--     foreign key, the seven CHECK/VALIDATE/SET NOT NULL triples, and the
--     compression round trip all work in this order. It did NOT cover the
--     primary-key statement below (d-6's step 6 was a CREATE UNIQUE INDEX),
--     nor the plpgsql wrapper, nor any rollback.
--   * The single-transaction property -- this exact chain inside one plpgsql
--     function, and an aborted call leaving the catalog byte-for-byte
--     unchanged -- is pinned by the node-27 throwaway integration run
--     (tasks 2.8) and by
--     tests/test_river_identity_normalization_integration.py, whose
--     negative-path test snapshots the catalog before and after a failing
--     call and asserts equality.
--
-- That rollback fidelity is what makes a single function safe here: there is
-- no half-cut-over state to clean up.
--
-- Why there is no cheap "prepare outside the window" stage: with
-- `timescaledb.compress = true` in force, TimescaleDB 2.10.2 rejects
-- ADD CONSTRAINT CHECK, VALIDATE CONSTRAINT, SET NOT NULL, CREATE UNIQUE
-- INDEX, and DROP CONSTRAINT alike, with
-- `operation not supported on hypertables that have compression enabled` --
-- measured with ZERO compressed chunks present, so it is the setting and not
-- the data that blocks them. Disabling compression requires every chunk to be
-- decompressed first. There is therefore nothing that can usefully be done
-- ahead of the window, and the pre-built-index trick is unavailable anyway:
-- `ADD CONSTRAINT ... PRIMARY KEY USING INDEX` fails on hypertables
-- (`hypertables do not support adding a constraint using an existing index`),
-- `CREATE INDEX CONCURRENTLY` is rejected, and
-- `WITH (timescaledb.transaction_per_chunk)` is incompatible with UNIQUE.
-- The primary-key index build happens inside the window and is the dominant
-- cost of the whole operation.
--
-- The zero-NULL gate is `VALIDATE CONSTRAINT`, not a counting query. A single
-- NULL anywhere in the seven columns makes VALIDATE raise, which aborts the
-- function's transaction and reverts everything -- fail-closed by
-- construction rather than by a check that could be forgotten. Negative path
-- as exercised by the integration test named above: raises
-- `check constraint "..." of relation "_hyper_N_M_chunk" is violated by some
-- row`, after which compression, the text FK, and the old primary key are all
-- still in place and no CHECK constraint is left behind.
--
-- Dropping the text foreign key is MANDATORY, not a design preference:
-- TimescaleDB 2.10.2 refuses a compression configuration that does not cover
-- the FK's columns (`column "river_segment_id" must be used for segmenting`),
-- and the target segmentby is integer-only. This retires that FK ahead of the
-- text columns it belongs to -- recorded as a deviation, and in the ADR 0002
-- amendment, because it is a real loss of referential enforcement between
-- this change and the column-retirement issue.

CREATE OR REPLACE FUNCTION hydro.cutover_river_identity_normalization()
RETURNS void
LANGUAGE plpgsql
AS $cutover$
DECLARE
  target_columns CONSTANT text[] := ARRAY[
    'run_key', 'river_network_version_key', 'basin_version_key',
    'river_segment_key', 'variable_e', 'unit_e', 'quality_flag_e'
  ];
  target_pkey CONSTANT text :=
    'run_key, river_network_version_key, river_segment_key, variable_e, valid_time';
  compressed_chunks bigint;
  current_pkey text;
  fk_name text;
  pkey_name text;
  column_name text;
BEGIN
  -- Already cut over? Report and return without touching anything. A second
  -- invocation must not error out with a confusing "constraint already
  -- exists" from the middle of the chain.
  SELECT string_agg(att.attname, ', ' ORDER BY k.ord)
    INTO current_pkey
    FROM pg_constraint AS con,
         unnest(con.conkey) WITH ORDINALITY AS k(attnum, ord),
         pg_attribute AS att
   WHERE con.conrelid = 'hydro.river_timeseries'::regclass
     AND con.contype = 'p'
     AND att.attrelid = con.conrelid
     AND att.attnum = k.attnum;

  IF current_pkey = target_pkey THEN
    RAISE NOTICE 'river_timeseries identity cutover already applied (primary key is %); nothing to do', target_pkey;
    RETURN;
  END IF;

  -- Precondition (catalog only, no table scan): zero compressed chunks.
  -- `SET (timescaledb.compress = false)` would refuse anyway, but refusing
  -- here names the actual problem and the actual remedy.
  SELECT count(*)
    INTO compressed_chunks
    FROM timescaledb_information.chunks AS c
   WHERE c.hypertable_schema = 'hydro'
     AND c.hypertable_name = 'river_timeseries'
     AND c.is_compressed;

  IF compressed_chunks > 0 THEN
    RAISE EXCEPTION
      'cutover refused: % compressed chunk(s) on hydro.river_timeseries; decompress every chunk first '
      '(runbook section 4.6, decompression-replay runner) -- TimescaleDB 2.10 cannot disable compression '
      'while any chunk is compressed', compressed_chunks;
  END IF;

  -- Unlock the DDL that `timescaledb.compress = true` forbids.
  ALTER TABLE hydro.river_timeseries SET (timescaledb.compress = false);

  -- Drop the two-column text foreign key to core.river_segment. Looked up by
  -- catalog rather than by literal name: the default name is exactly 63
  -- characters, i.e. at PostgreSQL's identifier limit, so it is one rename or
  -- one truncation away from not matching a hardcoded string.
  SELECT con.conname
    INTO fk_name
    FROM pg_constraint AS con
   WHERE con.conrelid = 'hydro.river_timeseries'::regclass
     AND con.contype = 'f'
     AND con.confrelid = 'core.river_segment'::regclass;

  IF fk_name IS NULL THEN
    RAISE NOTICE 'no text foreign key to core.river_segment found; assuming it was already retired';
  ELSE
    EXECUTE format('ALTER TABLE hydro.river_timeseries DROP CONSTRAINT %I', fk_name);
  END IF;

  -- Seven NOT NULL check constraints, added invalid then validated. VALIDATE
  -- is the zero-NULL gate (it scans; ~0.5 s per column per 3M rows measured,
  -- so budget on the order of ten minutes for seven columns at 460M rows).
  -- The subsequent SET NOT NULL then skips its own scan because a validated
  -- constraint already proves the property -- measured 0.59-0.79 ms against
  -- 1161 ms for the same statement without one.
  FOREACH column_name IN ARRAY target_columns LOOP
    EXECUTE format(
      'ALTER TABLE hydro.river_timeseries ADD CONSTRAINT %I CHECK (%I IS NOT NULL) NOT VALID',
      'river_timeseries_' || column_name || '_not_null', column_name);
    EXECUTE format(
      'ALTER TABLE hydro.river_timeseries VALIDATE CONSTRAINT %I',
      'river_timeseries_' || column_name || '_not_null');
    EXECUTE format(
      'ALTER TABLE hydro.river_timeseries ALTER COLUMN %I SET NOT NULL', column_name);
  END LOOP;

  -- Swap the primary key. The CHECK constraints above are deliberately kept:
  -- they cost a trivial per-row evaluation on insert and leave an auditable
  -- catalog trace of how the NOT NULLs were established.
  SELECT con.conname
    INTO pkey_name
    FROM pg_constraint AS con
   WHERE con.conrelid = 'hydro.river_timeseries'::regclass
     AND con.contype = 'p';

  IF pkey_name IS NULL THEN
    RAISE EXCEPTION 'cutover refused: hydro.river_timeseries has no primary key to replace';
  END IF;

  EXECUTE format('ALTER TABLE hydro.river_timeseries DROP CONSTRAINT %I', pkey_name);
  EXECUTE format(
    'ALTER TABLE hydro.river_timeseries ADD CONSTRAINT river_timeseries_pkey PRIMARY KEY (%s)',
    target_pkey);

  -- Re-enable compression on the normalized columns. TimescaleDB 2.10 requires
  -- segmentby U orderby to cover every primary-key column, which is why this
  -- and the primary-key swap above cannot be separated.
  ALTER TABLE hydro.river_timeseries SET (
    timescaledb.compress = true,
    timescaledb.compress_segmentby = 'run_key, river_network_version_key, river_segment_key',
    timescaledb.compress_orderby = 'variable_e, valid_time'
  );

  RAISE NOTICE 'river_timeseries identity cutover complete; primary key is now (%)', target_pkey;
END;
$cutover$;

COMMENT ON FUNCTION hydro.cutover_river_identity_normalization() IS
  'Maintenance-window primary-key + compression-settings switch for issue '
  '#1339. Single transaction, fail-closed: refuses while any chunk is '
  'compressed, and aborts (reverting everything) if any of the seven '
  'normalized columns still contains NULL. Requires ingest paused and every '
  'chunk decompressed. Never invoked by the migration chain -- see runbook '
  'docs/runbooks/tier-node27-timeseries-storage.md section 4.6.';
