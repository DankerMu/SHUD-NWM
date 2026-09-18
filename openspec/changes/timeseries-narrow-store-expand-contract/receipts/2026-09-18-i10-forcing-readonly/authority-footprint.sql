\timing on
BEGIN READ ONLY;
SET LOCAL statement_timeout = '180s';

\echo '=== A. authority table row counts ==='
SELECT 'met.met_station' AS t, count(*) AS rows FROM met.met_station
UNION ALL SELECT 'met.forcing_version', count(*) FROM met.forcing_version;

\echo '=== B. index footprint (authority tables) ==='
SELECT c.relname AS table_name, i.relname AS index_name,
       pg_size_pretty(pg_relation_size(i.oid)) AS index_size,
       pg_get_indexdef(i.oid) AS def
  FROM pg_class c
  JOIN pg_index x ON x.indrelid = c.oid
  JOIN pg_class i ON i.oid = x.indexrelid
  JOIN pg_namespace n ON n.oid = c.relnamespace
 WHERE n.nspname='met' AND c.relname IN ('met_station','forcing_version')
 ORDER BY c.relname, i.relname;

\echo '=== C. authority table heap/total size ==='
SELECT relname,
       pg_size_pretty(pg_table_size(c.oid))  AS heap,
       pg_size_pretty(pg_indexes_size(c.oid)) AS indexes,
       pg_size_pretty(pg_total_relation_size(c.oid)) AS total
  FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
 WHERE n.nspname='met' AND c.relname IN ('met_station','forcing_version');

\echo '=== D. fact table scale ==='
SELECT count(*) AS forcing_version_rows_with_facts FROM (
  SELECT DISTINCT forcing_version_id FROM met.forcing_version
) s;
SELECT hypertable_name, count(*) AS chunks,
       pg_size_pretty(sum(total_bytes)) AS total
  FROM timescaledb_information.chunks c
  JOIN LATERAL (SELECT total_bytes FROM chunks_detailed_size(format('%I.%I', c.hypertable_schema, c.hypertable_name)) d
                WHERE d.chunk_name = c.chunk_name) z ON true
 WHERE c.hypertable_schema='met' AND c.hypertable_name='forcing_station_timeseries'
 GROUP BY hypertable_name;
COMMIT;
