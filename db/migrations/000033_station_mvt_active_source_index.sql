CREATE INDEX CONCURRENTLY IF NOT EXISTS met_station_active_basin_station_idx
  ON met.met_station (basin_version_id, station_id)
  WHERE active_flag = true;
