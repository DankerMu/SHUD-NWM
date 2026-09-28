\pset pager off
BEGIN READ ONLY;
SELECT c.relname, c.reltuples::bigint, c.relpages, s.n_live_tup, s.last_analyze, s.last_autoanalyze, s.n_mod_since_analyze
FROM pg_class c JOIN pg_stat_all_tables s ON s.relid = c.oid
WHERE c.relname IN ('_hyper_9_206_chunk','_hyper_9_213_chunk','_hyper_9_217_chunk','_hyper_9_221_chunk','_hyper_9_225_chunk','_hyper_9_231_chunk','_hyper_9_233_chunk','_hyper_9_237_chunk')
ORDER BY c.relname;
ROLLBACK;
