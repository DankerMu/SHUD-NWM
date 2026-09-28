\pset pager off
BEGIN READ ONLY;
SET LOCAL statement_timeout='60s';
-- F1 third basin: missing candidates (run_key < 0, no rows) whose window [cycle, cycle+7d]
-- lies on the newest uncompressed chunks. :cands is a VALUES list (run_key, cycle_time, end_time).
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
CROSS JOIN LATERAL (
    SELECT 1 FROM hydro.river_timeseries rt
    WHERE rt.run_key = o.run_key
      AND rt.river_segment_key = seg.river_segment_key
      AND rt.basin_version_key IS NOT NULL
      AND rt.basin_version_key IS NOT DISTINCT FROM seg.basin_version_key
      AND rt.river_network_version_key IS NOT NULL
      AND rt.river_network_version_key IS NOT DISTINCT FROM seg.river_network_version_key
      AND rt.variable_e = 'q_down'::hydro.river_variable
      AND rt.valid_time >= o.cycle_time
      AND rt.valid_time <= o.end_time
    LIMIT 1
) hit;
-- Rows of this segment (all runs) in each hot chunk inside the window: the index
-- river_ts_segment_time_key_idx has to walk these when run_key is only a Filter.
SELECT tableoid::regclass AS chunk, count(*) AS seg_q_down_rows, count(DISTINCT run_key) AS runs
FROM hydro.river_timeseries rt
WHERE rt.river_segment_key = (SELECT river_segment_key FROM core.river_segment WHERE river_segment_id = :'segid' AND river_network_version_id = :'net')
  AND rt.variable_e = 'q_down'::hydro.river_variable
  AND rt.valid_time >= :'win_lo'::timestamptz AND rt.valid_time <= :'win_hi'::timestamptz
GROUP BY 1 ORDER BY 1::text;
ROLLBACK;
