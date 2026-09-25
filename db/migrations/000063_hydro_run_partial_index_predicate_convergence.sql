-- Convergence (#2048). Commit b97c16e2 rewrote the status predicates of six
-- partial indexes inside their already-applied migrations (000021, 000024,
-- 000030, 000031_search_discovery_performance, 000040). Production kept the
-- pre-b97c16e2 predicates: four of the six do not contain 'succeeded', so a
-- query filtering status IN ('succeeded', 'parsed', 'published') can never use
-- them, and the other two still carry 'frequency_done'.
--
-- Each index is dropped and recreated with exactly the column list and
-- predicate of its defining migration (tests/test_migrations.py pins the copy).
-- The rebuild is unconditional: on a fresh database the indexes are already
-- correct and rebuilding a small table costs nothing, while a conditional
-- would need a DO block, which CONCURRENTLY cannot run inside.
--
-- CONCURRENTLY for all six, including the two whose defining migrations used a
-- plain CREATE INDEX: hydro.hydro_run is a plain table, not a hypertable, and
-- the runner autocommits each statement, which CONCURRENTLY requires.
--
-- Failure and rerun: a CREATE INDEX CONCURRENTLY that fails (for example on
-- lock_timeout while it waits for an older snapshot) leaves an INVALID index and
-- no ledger row, so the next run replays this whole file. Every pair runs again,
-- and the failed index's DROP INDEX CONCURRENTLY IF EXISTS removes the INVALID
-- copy before its rebuild; the end state equals a clean run. That is why each
-- DROP precedes its CREATE rather than a create-then-rename scheme. Between a
-- DROP and its CREATE the planner has one index fewer; at this table's size the
-- fallback is a sequential scan of about the same cost.

-- 000021
DROP INDEX CONCURRENTLY IF EXISTS hydro.hydro_run_latest_ready_run_idx;
CREATE INDEX CONCURRENTLY IF NOT EXISTS hydro_run_latest_ready_run_idx
  ON hydro.hydro_run (cycle_time DESC, run_id DESC)
  WHERE status IN ('succeeded', 'parsed', 'published');

-- 000024
DROP INDEX CONCURRENTLY IF EXISTS hydro.hydro_run_qhh_latest_candidate_idx;
CREATE INDEX CONCURRENTLY IF NOT EXISTS hydro_run_qhh_latest_candidate_idx
  ON hydro.hydro_run (LOWER(source_id), run_type, basin_version_id, cycle_time DESC, run_id DESC)
  WHERE cycle_time IS NOT NULL
    AND status IN ('succeeded', 'parsed', 'published');

-- 000030
DROP INDEX CONCURRENTLY IF EXISTS hydro.hydro_run_qhh_latest_candidate_parsed_idx;
CREATE INDEX CONCURRENTLY IF NOT EXISTS hydro_run_qhh_latest_candidate_parsed_idx
  ON hydro.hydro_run (LOWER(source_id), run_type, basin_version_id, cycle_time DESC, run_id DESC)
  WHERE cycle_time IS NOT NULL
    AND status IN ('succeeded', 'parsed', 'published');

-- 000031_search_discovery_performance
DROP INDEX CONCURRENTLY IF EXISTS hydro.hydro_run_display_product_basin_status_idx;
CREATE INDEX CONCURRENTLY IF NOT EXISTS hydro_run_display_product_basin_status_idx
  ON hydro.hydro_run (basin_version_id, status)
  WHERE status IN ('succeeded', 'parsed', 'published');

-- 000040
DROP INDEX CONCURRENTLY IF EXISTS hydro.hydro_run_display_ready_candidate_idx;
CREATE INDEX CONCURRENTLY IF NOT EXISTS hydro_run_display_ready_candidate_idx
  ON hydro.hydro_run (LOWER(source_id), run_type, basin_version_id, cycle_time DESC, run_id DESC)
  WHERE cycle_time IS NOT NULL
    AND status IN ('succeeded', 'parsed', 'published');

-- 000040
DROP INDEX CONCURRENTLY IF EXISTS hydro.hydro_run_display_ready_basin_status_idx;
CREATE INDEX CONCURRENTLY IF NOT EXISTS hydro_run_display_ready_basin_status_idx
  ON hydro.hydro_run (basin_version_id, status)
  WHERE status IN ('succeeded', 'parsed', 'published');
