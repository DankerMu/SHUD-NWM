CREATE INDEX IF NOT EXISTS return_period_result_valid_time_discovery_idx
  ON flood.return_period_result (run_id, duration, max_over_window, valid_time DESC);

CREATE INDEX IF NOT EXISTS river_timeseries_valid_time_discovery_idx
  ON hydro.river_timeseries (run_id, variable, valid_time DESC);
