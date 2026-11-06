\timing on
BEGIN READ ONLY;
SET LOCAL statement_timeout = '600s';
\echo '=== D. per-forcing_version existence probe, all 8653, one statement ==='
EXPLAIN (ANALYZE, BUFFERS, TIMING ON)
SELECT fv.forcing_version_id,
       EXISTS (SELECT 1 FROM met.forcing_station_timeseries f
                WHERE f.forcing_version_id = fv.forcing_version_id) AS has_legacy_rows
  FROM met.forcing_version fv;
\echo '=== D2. how many actually have rows ==='
SELECT has_legacy_rows, count(*) FROM (
  SELECT EXISTS (SELECT 1 FROM met.forcing_station_timeseries f
                  WHERE f.forcing_version_id = fv.forcing_version_id) AS has_legacy_rows
    FROM met.forcing_version fv) s
GROUP BY 1;
COMMIT;
