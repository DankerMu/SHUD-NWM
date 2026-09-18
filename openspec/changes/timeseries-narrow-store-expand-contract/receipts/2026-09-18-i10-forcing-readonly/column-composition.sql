BEGIN READ ONLY;
SET LOCAL statement_timeout = '180s';
SELECT count(*) AS rows,
       pg_size_pretty(avg(pg_column_size(properties_json))::bigint) AS avg_props,
       pg_size_pretty(max(pg_column_size(properties_json))::bigint) AS max_props,
       pg_size_pretty(avg(pg_column_size(geom))::bigint)            AS avg_geom,
       pg_size_pretty(avg(pg_column_size(station_id) + pg_column_size(basin_version_id)
                        + pg_column_size(coalesce(station_name,'')))::bigint) AS avg_text,
       pg_size_pretty(sum(pg_column_size(properties_json))::bigint) AS total_props
  FROM met.met_station;
SELECT pg_size_pretty(pg_relation_size(c.oid)) AS heap_only,
       pg_size_pretty(pg_total_relation_size(reltoastrelid)) AS toast
  FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
 WHERE n.nspname='met' AND c.relname='met_station';
COMMIT;
