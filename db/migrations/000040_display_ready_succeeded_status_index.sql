CREATE INDEX CONCURRENTLY IF NOT EXISTS hydro_run_display_ready_candidate_idx
  ON hydro.hydro_run (LOWER(source_id), run_type, basin_version_id, cycle_time DESC, run_id DESC)
  WHERE cycle_time IS NOT NULL
    AND status IN ('succeeded', 'parsed', 'frequency_done', 'published');

CREATE INDEX CONCURRENTLY IF NOT EXISTS hydro_run_display_ready_basin_status_idx
  ON hydro.hydro_run (basin_version_id, status)
  WHERE status IN ('succeeded', 'parsed', 'frequency_done', 'published');
