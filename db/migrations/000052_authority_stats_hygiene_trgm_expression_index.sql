-- Change `authority-stats-hygiene-trgm-equality-trap` (issue #1468). Rebuilds
-- the identifier trigram index as an EXPRESSION index and gives the four core
-- identity tables per-table autovacuum analyze parameters.
--
-- ---------------------------------------------------------------------------
-- THE TRAP (measured, node-27 production `nhms`, 2026-08-21, PG 15.2 /
-- pg_trgm 1.6; full receipt in issue #1468)
-- ---------------------------------------------------------------------------
--
-- Since pg_trgm 1.6 the `gin_trgm_ops` operator class supports `=`, so the
-- planner treats a trigram GIN as a candidate for EQUALITY lookups, not only
-- for LIKE/ILIKE. On an identifier column whose values share a ~34 character
-- prefix (`basins_jialingjiang_shud_shud_riv_`: 14,673 rows;
-- `basins_zhaochen_*`: ~9k rows) the posting lists of those leading trigrams
-- cover nearly the whole table, and the GIN cost model badly underestimates
-- the work. Measured on 2,000 equality lookups of the shape
-- `rs.river_segment_id = t.river_segment_id AND rs.river_network_version_id =
-- t.river_network_version_id`:
--
--   default planner                 Bitmap Index Scan, trigram index
--                                   51,029 ms / 2,560,171 buffers
--   `enable_bitmapscan = off`       Index Only Scan, the primary key
--                                        17 ms /     9,718 buffers
--
-- Estimated cost was 0.72 (trigram) against 2.28 (primary key), so the planner
-- picked the 2,900x slower plan on purpose. FRESH STATISTICS DO NOT FIX THIS:
-- the numbers above were sampled after two manual ANALYZE runs (2026-08-16 and
-- 2026-08-19). Neither does a session knob -- `enable_bitmapscan = off` only
-- rescues the one consumer that remembers to set it.
--
-- The fix is STRUCTURAL, not a cost negotiation: an index built on the
-- expression `lower(river_segment_id)` cannot be matched by a predicate over
-- the bare column at all, so `river_segment_id = $1` is no longer a candidate
-- regardless of statistics or cost. Search keeps its index by spelling the same
-- expression (`lower(rs.river_segment_id) LIKE <lowercased pattern> ESCAPE`);
-- see packages/common/model_registry.py and ADR 0004.
--
-- `lower()` rather than `col || ''`: it is the standard PostgreSQL
-- case-insensitive expression-index form, and ILIKE already matched lowercased
-- trigrams inside the GIN, so the hit set is unchanged for ASCII slug ids.
--
-- ---------------------------------------------------------------------------
-- SHAPE: idempotent rename/build/drop swap, deliberately outside any transaction
-- ---------------------------------------------------------------------------
--
-- `CREATE/DROP INDEX CONCURRENTLY` cannot run inside a transaction block, so
-- this file contains no BEGIN/COMMIT. `packages/common/migrate.apply_migration`
-- executes each statement on an autocommit connection and its splitter keeps
-- dollar-quoted bodies intact, so the DO block below arrives whole.
--
-- NO STATEMENT IN THIS FILE TAKES ACCESS EXCLUSIVE ON `core.river_segment`.
-- The table serves live MVT reads; a lock that queues behind them would block
-- every new reader until this file's `lock_timeout` fires and then abort the
-- migration. Every index removal is therefore CONCURRENTLY, and every index
-- displacement is a RENAME (SHARE UPDATE EXCLUSIVE, readers unaffected).
--
--   0. `DROP INDEX CONCURRENTLY IF EXISTS core.river_segment_id_trgm_idx_invalid`
--      -- clears the leftover of a PREVIOUS interrupted run, so the rename in
--      step 1 cannot collide with a name that is already taken.
--   1. DO block (`SET LOCAL lock_timeout = '2s'`, precedent 000050): rename a
--      same-named INVALID index left by an interrupted concurrent build to
--      `..._invalid`, then rename the bare-column index out of the way as
--      `..._legacy`. Renaming rather than dropping the INVALID leftover is not
--      cosmetic: an INVALID index has no readers, but a NON-CONCURRENT
--      `DROP INDEX` still takes ACCESS EXCLUSIVE on the TABLE, which is exactly
--      what the paragraph above forbids. The `_legacy` rename's purpose is
--      IDEMPOTENCE (a second pass can tell the old bare-column index apart from
--      the freshly built expression index and will not delete the new one).
--   2. `CREATE INDEX CONCURRENTLY IF NOT EXISTS` the expression index.
--   3. `DROP INDEX CONCURRENTLY IF EXISTS` both renamed leftovers.
--
-- During the seconds-long build window search falls back to
-- bitmap(river_network_version) + Filter (measured ~20 ms, issue #1468
-- read-only diagnosis) -- the `_legacy` index is a bare-column GIN and cannot
-- serve the rewritten `lower(...) LIKE` predicate anyway.
--
-- RECOVERY AFTER AN INTERRUPTED CONCURRENT BUILD: re-run this file. A cancelled
-- `CREATE INDEX CONCURRENTLY` leaves an INVALID index behind, and
-- `IF NOT EXISTS` alone would happily skip it (it matches by name only), which
-- is why step 1 moves the leftover aside as `..._invalid` before rebuilding and
-- step 3 drops it concurrently. Every interruption point is replayable: after
-- step 0 or 1 the second pass renames what is still there and builds; after
-- step 2 it only drops the leftovers; if the run dies between the two drops of
-- step 3, step 0 of the next pass removes the `_invalid` one. After step 3 only
-- the ALTERs remain, and those are idempotent.
--
-- ---------------------------------------------------------------------------
-- WHY THE per-table AUTOVACUUM PARAMETERS ARE HERE TOO
-- ---------------------------------------------------------------------------
--
-- Two independent statistics failures hit these tables (design D1). This file
-- covers the CHURN one: a new 5,000-segment network is far below the default
-- 209k x 10% threshold, and a single-row change to a 20-row version table is
-- below the flat 50. The other one -- a crash recovery discarding all
-- cumulative statistics, after which a zero-churn table never crosses ANY
-- threshold -- is covered by the autopipe stats guard's repair leg
-- (scripts/node27_autopipeline.py) and runbook section 4.8.

DROP INDEX CONCURRENTLY IF EXISTS core.river_segment_id_trgm_idx_invalid;

DO $$
DECLARE
    invalid_leftover BOOLEAN;
    bare_column_index BOOLEAN;
BEGIN
    SET LOCAL lock_timeout = '2s';

    SELECT EXISTS (
        SELECT 1
        FROM pg_index ix, pg_class idx, pg_class tbl, pg_namespace ns
        WHERE ix.indexrelid = idx.oid
          AND ix.indrelid = tbl.oid
          AND tbl.relnamespace = ns.oid
          AND ns.nspname = 'core'
          AND tbl.relname = 'river_segment'
          AND idx.relname = 'river_segment_id_trgm_idx'
          AND ix.indisvalid = false
    ) INTO invalid_leftover;

    -- An INVALID index has no readers (the planner ignores it), but dropping it
    -- non-concurrently would still take ACCESS EXCLUSIVE on core.river_segment
    -- and block live MVT readers -- so move it aside instead and let step 3
    -- drop it concurrently. The rename takes SHARE UPDATE EXCLUSIVE.
    IF invalid_leftover THEN
        ALTER INDEX core.river_segment_id_trgm_idx
            RENAME TO river_segment_id_trgm_idx_invalid;
    END IF;

    SELECT EXISTS (
        SELECT 1
        FROM pg_indexes
        WHERE schemaname = 'core'
          AND indexname = 'river_segment_id_trgm_idx'
          AND indexdef NOT LIKE '%lower(%'
    ) INTO bare_column_index;

    IF bare_column_index THEN
        ALTER INDEX core.river_segment_id_trgm_idx
            RENAME TO river_segment_id_trgm_idx_legacy;
    END IF;
END $$;

CREATE INDEX CONCURRENTLY IF NOT EXISTS river_segment_id_trgm_idx
  ON core.river_segment USING GIN (lower(river_segment_id) gin_trgm_ops);

DROP INDEX CONCURRENTLY IF EXISTS core.river_segment_id_trgm_idx_legacy;

DROP INDEX CONCURRENTLY IF EXISTS core.river_segment_id_trgm_idx_invalid;

-- 209k / 155k row tables: a new network (~5k segments) must cross the bar.
ALTER TABLE core.river_segment
  SET (autovacuum_analyze_scale_factor = 0.01, autovacuum_analyze_threshold = 500);

ALTER TABLE core.river_segment_crosswalk
  SET (autovacuum_analyze_scale_factor = 0.01, autovacuum_analyze_threshold = 500);

-- 20-row version tables: any single-row change must cross the bar.
ALTER TABLE core.river_network_version
  SET (autovacuum_analyze_scale_factor = 0, autovacuum_analyze_threshold = 1);

ALTER TABLE core.basin_version
  SET (autovacuum_analyze_scale_factor = 0, autovacuum_analyze_threshold = 1);
