\pset pager off
BEGIN READ ONLY;
SET LOCAL statement_timeout = '60s';
SELECT basin_version_id, count(*) AS forecast_runs, count(DISTINCT source_id) AS sources, max(cycle_time) AS max_cycle
FROM hydro.hydro_run WHERE run_type = 'forecast' AND cycle_time IS NOT NULL
GROUP BY basin_version_id ORDER BY forecast_runs DESC LIMIT 12;
SELECT lower(source_id) AS src, extract(hour from cycle_time AT TIME ZONE 'UTC') AS hh, end_time - cycle_time AS win, count(*)
FROM hydro.hydro_run WHERE run_type = 'forecast' GROUP BY 1,2,3 ORDER BY 1,2,3;
SELECT lower(source_id) AS src, max(cycle_time) FROM hydro.hydro_run WHERE run_type='forecast' GROUP BY 1;
ROLLBACK;
