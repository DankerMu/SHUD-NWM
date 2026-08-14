-- The MVT identity lookup index carries the same column SET as the primary key
-- (run_id, river_network_version_id, river_segment_id, variable, valid_time) in a
-- different order, so it duplicates pkey coverage on every Timescale chunk. Live
-- node-27 measurement 2026-08-14 (8 chunks aggregated): 162 GB for 5,571 idx_scan
-- against 796,096,944 pkey scans over the same window. `variable` is single-valued
-- table-wide, so leading with it buys zero selectivity and the planner keeps
-- choosing the pkey. Rollback (verbatim re-create, from 000019):
--   CREATE INDEX IF NOT EXISTS river_timeseries_mvt_identity_lookup_idx ON hydro.river_timeseries (run_id, variable, valid_time, river_network_version_id, river_segment_id);
DROP INDEX CONCURRENTLY IF EXISTS hydro.river_timeseries_mvt_identity_lookup_idx;

-- The valid-time discovery index (run_id, variable, valid_time DESC) is a strict
-- prefix of the index dropped above -- btree scans backwards, so the DESC direction
-- is not a distinction -- and measured 4663 MB for 10,864 idx_scan. Its residual
-- discovery use is served by the pkey prefix plus the single-valued `variable`
-- filter. No in-repo migration ever created it: it was created out-of-band directly
-- on node-27, so this file drops an object the migration chain never produced. That
-- makes IF EXISTS load-bearing rather than decorative -- on any database rebuilt
-- from the migration chain (CI, hermetic test DBs, future nodes) the index never
-- existed and this statement must replay as a no-op. Rollback (verbatim re-create,
-- captured from pg_get_indexdef on node-27, 2026-08-14):
--   CREATE INDEX river_timeseries_valid_time_discovery_idx ON hydro.river_timeseries USING btree (run_id, variable, valid_time DESC);
DROP INDEX CONCURRENTLY IF EXISTS hydro.river_timeseries_valid_time_discovery_idx;
