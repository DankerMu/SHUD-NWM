BEGIN READ ONLY;
SELECT c.hypertable_name,
       c.chunk_name,
       c.is_compressed,
       c.range_start,
       c.range_end,
       s.last_analyze,
       s.last_autoanalyze,
       s.n_mod_since_analyze,
       s.n_live_tup
  FROM timescaledb_information.chunks c
  LEFT JOIN pg_stat_all_tables s
    ON s.schemaname = c.chunk_schema AND s.relname = c.chunk_name
 WHERE c.hypertable_schema = 'hydro'
   AND c.hypertable_name = 'river_timeseries_legacy'
 ORDER BY c.range_start;
COMMIT;
