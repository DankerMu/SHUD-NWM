CREATE INDEX IF NOT EXISTS hydro_run_qhh_latest_candidate_idx
  ON hydro.hydro_run (LOWER(source_id), run_type, basin_version_id, cycle_time DESC, run_id DESC)
  WHERE cycle_time IS NOT NULL
    AND status IN ('frequency_done', 'published');

CREATE INDEX IF NOT EXISTS basin_version_qhh_latest_lookup_idx
  ON core.basin_version (basin_id, basin_version_id);

CREATE INDEX IF NOT EXISTS forcing_station_timeseries_qhh_latest_window_idx
  ON met.forcing_station_timeseries (
    forcing_version_id,
    basin_version_id,
    LOWER(source_id),
    variable,
    valid_time DESC,
    station_id
  );

CREATE INDEX IF NOT EXISTS interp_weight_qhh_latest_membership_idx
  ON met.interp_weight (
    model_id,
    station_id,
    variable,
    LOWER(source_id)
  );

CREATE INDEX IF NOT EXISTS river_timeseries_qhh_latest_window_idx
  ON hydro.river_timeseries (
    run_id,
    basin_version_id,
    river_network_version_id,
    variable,
    valid_time DESC,
    river_segment_id
  );
