\pset pager off
BEGIN READ ONLY;
SELECT count(*) AS forecast_runs,
       count(*) FILTER (WHERE start_time IS DISTINCT FROM cycle_time) AS start_ne_cycle,
       count(*) FILTER (WHERE end_time IS NULL OR cycle_time IS NULL) AS null_bounds,
       count(*) FILTER (WHERE end_time <= cycle_time) AS end_le_cycle
FROM hydro.hydro_run WHERE run_type = 'forecast';
SELECT end_time - cycle_time AS window, count(*) FROM hydro.hydro_run WHERE run_type = 'forecast' GROUP BY 1 ORDER BY 1;
SELECT count(*) AS all_runs FROM hydro.hydro_run;
ROLLBACK;
