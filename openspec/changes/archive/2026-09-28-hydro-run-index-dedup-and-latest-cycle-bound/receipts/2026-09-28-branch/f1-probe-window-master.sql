\pset pager off
BEGIN READ ONLY;
SET LOCAL statement_timeout='60s';
-- F1 master shape (origin/master aff6201e0): unbounded EXISTS probe, same missing candidates as f1-probe-window.sql.
EXPLAIN (ANALYZE, BUFFERS, COSTS OFF)
WITH seg AS (
    SELECT
        (SELECT basin_version_key FROM core.basin_version WHERE basin_version_id = :'bv') AS basin_version_key,
        (SELECT river_segment_key FROM core.river_segment WHERE river_segment_id = :'segid' AND river_network_version_id = :'net') AS river_segment_key,
        (SELECT river_network_version_key FROM core.river_network_version WHERE river_network_version_id = :'net') AS river_network_version_key
)
SELECT o.cycle_time
FROM (VALUES :cands) o(run_key, cycle_time, end_time)
CROSS JOIN seg
WHERE EXISTS (
    SELECT 1 FROM hydro.river_timeseries rt
    WHERE rt.run_key = o.run_key
      AND rt.river_segment_key = seg.river_segment_key
      AND rt.basin_version_key IS NOT NULL
      AND rt.basin_version_key IS NOT DISTINCT FROM seg.basin_version_key
      AND rt.river_network_version_key IS NOT NULL
      AND rt.river_network_version_key IS NOT DISTINCT FROM seg.river_network_version_key
      AND rt.variable_e = 'q_down'::hydro.river_variable
);
ROLLBACK;
