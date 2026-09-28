\pset pager off
BEGIN READ ONLY;
SELECT now() AS measured_at, current_user, current_database();
SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = 'hydro' AND tablename = 'hydro_run' ORDER BY 1;
SELECT count(*) AS new_name_exists FROM pg_class WHERE relname = 'hydro_run_forecast_basin_cycle_idx';
SELECT relpages, relallvisible, reltuples FROM pg_class WHERE oid = 'hydro.hydro_run'::regclass;
SELECT n_live_tup, n_dead_tup, last_autovacuum, last_autoanalyze FROM pg_stat_user_tables WHERE relid = 'hydro.hydro_run'::regclass;
ROLLBACK;
