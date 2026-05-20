CREATE INDEX IF NOT EXISTS river_timeseries_mvt_identity_lookup_idx
  ON hydro.river_timeseries (run_id, variable, valid_time, river_network_version_id, river_segment_id);
