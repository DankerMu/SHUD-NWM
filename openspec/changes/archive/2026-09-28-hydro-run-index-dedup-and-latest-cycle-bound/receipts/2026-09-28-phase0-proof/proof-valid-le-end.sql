\pset pager off
SET default_transaction_read_only=on;
SET statement_timeout='30min';
SET enable_hashjoin=off; SET enable_mergejoin=off; SET max_parallel_workers_per_gather=0;
\timing on
SELECT h.run_key, h.run_id, h.cycle_time, h.end_time FROM hydro.hydro_run h
CROSS JOIN LATERAL (SELECT 1 FROM hydro.river_timeseries rt WHERE rt.run_key=h.run_key AND rt.valid_time > h.end_time LIMIT 1) x
WHERE h.run_type='forecast' AND h.cycle_time IS NOT NULL;
