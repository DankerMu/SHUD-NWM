CREATE INDEX CONCURRENTLY IF NOT EXISTS hydro_run_qhh_latest_candidate_parsed_idx
  ON hydro.hydro_run (LOWER(source_id), run_type, basin_version_id, cycle_time DESC, run_id DESC)
  WHERE cycle_time IS NOT NULL
    AND status IN ('succeeded', 'parsed', 'published');
