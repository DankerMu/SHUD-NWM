CREATE INDEX IF NOT EXISTS hydro_run_latest_ready_run_idx
  ON hydro.hydro_run (cycle_time DESC, run_id DESC)
  WHERE status IN ('succeeded', 'parsed', 'published');

CREATE INDEX IF NOT EXISTS river_timeseries_mvt_selected_identity_lookup_idx
  ON hydro.river_timeseries (
    run_id,
    basin_version_id,
    river_network_version_id,
    variable,
    valid_time,
    river_segment_id
  );

CREATE INDEX IF NOT EXISTS river_timeseries_mvt_selected_identity_valid_time_discovery_idx
  ON hydro.river_timeseries (
    run_id,
    basin_version_id,
    river_network_version_id,
    variable,
    valid_time DESC
  );
