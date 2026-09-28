\pset pager off
BEGIN READ ONLY;
SET LOCAL statement_timeout='60s';
\set bv '''basins_byh_vbasins'''
\set seg '''basins_byh_shud_shud_riv_000001'''
\set net '''basins_byh_rivnet_vbasins'''
-- variant: :bounded = 0 (master), 1 (valid_time within [cycle_time, end_time])
EXPLAIN (ANALYZE, BUFFERS, COSTS OFF)
WITH seg AS (
    SELECT
        (SELECT basin_version_key FROM core.basin_version WHERE basin_version_id = :bv) AS basin_version_key,
        (SELECT river_segment_key FROM core.river_segment WHERE river_segment_id = :seg AND river_network_version_id = :net) AS river_segment_key,
        (SELECT river_network_version_key FROM core.river_network_version WHERE river_network_version_id = :net) AS river_network_version_key
),
cand AS MATERIALIZED (
    SELECT h.run_key, h.scenario_id, h.cycle_time, h.end_time
    FROM hydro.hydro_run h
    WHERE h.run_type = 'forecast' AND h.cycle_time IS NOT NULL AND h.basin_version_id = :bv
      AND (LOWER(h.source_id) = ANY(ARRAY['gfs','ifs']) OR LOWER(h.scenario_id) = ANY(ARRAY['forecast_gfs_deterministic','forecast_ifs_deterministic','gfs','ifs']))
),
scen AS (SELECT DISTINCT scenario_id FROM cand)
SELECT scen.scenario_id, pick.cycle_time
FROM scen CROSS JOIN seg
CROSS JOIN LATERAL (
    SELECT o.cycle_time
    FROM (SELECT c.run_key, c.cycle_time, c.end_time FROM cand c WHERE c.scenario_id = scen.scenario_id ORDER BY c.cycle_time DESC OFFSET 0) o
    CROSS JOIN LATERAL (
        SELECT 1 FROM hydro.river_timeseries rt
        WHERE rt.run_key = o.run_key
          AND rt.river_segment_key = seg.river_segment_key
          AND rt.basin_version_key IS NOT NULL
          AND rt.basin_version_key IS NOT DISTINCT FROM seg.basin_version_key
          AND rt.river_network_version_key IS NOT NULL
          AND rt.river_network_version_key IS NOT DISTINCT FROM seg.river_network_version_key
          AND rt.variable_e = 'q_down'::hydro.river_variable
          AND rt.valid_time >= o.cycle_time AND rt.valid_time <= o.end_time
            LIMIT 1
    ) hit
    ORDER BY o.cycle_time DESC LIMIT 1
) pick
ORDER BY scen.scenario_id;
ROLLBACK;
