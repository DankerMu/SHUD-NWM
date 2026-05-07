CREATE INDEX IF NOT EXISTS canonical_met_source_cycle_idx ON met.canonical_met_product (source_id, cycle_time, variable);
CREATE INDEX IF NOT EXISTS met_station_basin_idx ON met.met_station (basin_version_id);
CREATE INDEX IF NOT EXISTS river_ts_segment_time_idx ON hydro.river_timeseries (river_segment_id, variable, valid_time DESC);
