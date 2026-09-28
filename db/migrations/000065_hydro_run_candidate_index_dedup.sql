-- hydro.hydro_run index surface (#2634, #2626).
--
-- Dedup (#2634). After 000063 the ledger holds two byte-identical groups of
-- partial indexes (node-27 pg_indexes, 2026-09-28):
--   (LOWER(source_id), run_type, basin_version_id, cycle_time DESC, run_id DESC)
--     WHERE cycle_time IS NOT NULL AND status IN ('succeeded', 'parsed', 'published')
--     x3: hydro_run_qhh_latest_candidate_idx (000024),
--         hydro_run_qhh_latest_candidate_parsed_idx (000030),
--         hydro_run_display_ready_candidate_idx (000040);
--   (basin_version_id, status) WHERE status IN ('succeeded', 'parsed', 'published')
--     x2: hydro_run_display_product_basin_status_idx (000031),
--         hydro_run_display_ready_basin_status_idx (000040).
-- Every write to hydro_run maintains each copy; the planner gains nothing from a
-- second one. Kept: hydro_run_qhh_latest_candidate_idx (named by forecast_store's
-- QHH latest-product index evidence and its tests) and
-- hydro_run_display_product_basin_status_idx (the path node27_autopipeline's
-- publish transition takes). Dropped: the other three. Their defining migrations
-- and 000063 stay byte-for-byte as applied; this file is the forward step.
--
-- Status-free forecast candidate index (#2626). The #2424 D1 latest-cycle
-- discovery reads its candidates with no status predicate (user decision (a)),
-- so no status-partial index can serve it and it sequentially scanned hydro_run.
-- Its only sargable conjunct is the basin_version_id equality (the scenario
-- filter is a LOWER(...) disjunction). Column list chosen on a node-27 scratch
-- copy of hydro_run (openspec change hydro-run-index-dedup-and-latest-cycle-bound,
-- design D2): (basin_version_id, cycle_time DESC) touched the fewest shared
-- buffers under production's visibility map; an INCLUDE variant only won with
-- every page all-visible, which production's hydro_run is not.
--
-- CONCURRENTLY: hydro.hydro_run is a plain table, and the runner autocommits
-- each statement, which CONCURRENTLY requires. Online writers keep running.
--
-- Failure and rerun: a CREATE INDEX CONCURRENTLY that fails leaves an INVALID
-- index and no ledger row, so the next run replays this whole file. The three
-- DROPs are idempotent. The new index is dropped before it is created, so an
-- INVALID leftover is removed and rebuilt, and the end state equals a clean run;
-- a bare IF NOT EXISTS would silently keep the INVALID copy (#2048). Between the
-- DROP and the CREATE the discovery falls back to the sequential scan it uses
-- today.

DROP INDEX CONCURRENTLY IF EXISTS hydro.hydro_run_qhh_latest_candidate_parsed_idx;
DROP INDEX CONCURRENTLY IF EXISTS hydro.hydro_run_display_ready_candidate_idx;
DROP INDEX CONCURRENTLY IF EXISTS hydro.hydro_run_display_ready_basin_status_idx;

DROP INDEX CONCURRENTLY IF EXISTS hydro.hydro_run_forecast_basin_cycle_idx;
CREATE INDEX CONCURRENTLY hydro_run_forecast_basin_cycle_idx
  ON hydro.hydro_run (basin_version_id, cycle_time DESC)
  WHERE run_type = 'forecast' AND cycle_time IS NOT NULL;
