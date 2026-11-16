BEGIN READ ONLY;
SELECT c.chunk_name,
       c.is_compressed,
       c.range_start::date AS rs,
       c.range_end::date   AS re,
       s.last_analyze,
       s.last_autoanalyze,
       s.n_mod_since_analyze,
       s.n_live_tup
  FROM timescaledb_information.chunks c
  LEFT JOIN pg_stat_all_tables s
    ON s.schemaname = c.chunk_schema AND s.relname = c.chunk_name
 WHERE c.hypertable_schema = 'hydro'
   AND c.hypertable_name = 'river_timeseries'
 ORDER BY c.range_start DESC
 LIMIT 12;
COMMIT;
