CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE INDEX CONCURRENTLY IF NOT EXISTS river_segment_id_trgm_idx
  ON core.river_segment USING GIN (river_segment_id gin_trgm_ops);

CREATE INDEX CONCURRENTLY IF NOT EXISTS river_segment_name_trgm_idx
  ON core.river_segment USING GIN ((COALESCE(properties_json->>'name', '')) gin_trgm_ops);

CREATE INDEX CONCURRENTLY IF NOT EXISTS river_segment_segment_name_trgm_idx
  ON core.river_segment USING GIN ((COALESCE(properties_json->>'segment_name', '')) gin_trgm_ops);

CREATE INDEX CONCURRENTLY IF NOT EXISTS met_station_id_trgm_idx
  ON met.met_station USING GIN (station_id gin_trgm_ops)
  WHERE active_flag = true;

CREATE INDEX CONCURRENTLY IF NOT EXISTS met_station_name_trgm_idx
  ON met.met_station USING GIN ((COALESCE(station_name, '')) gin_trgm_ops)
  WHERE active_flag = true;

CREATE INDEX CONCURRENTLY IF NOT EXISTS hydro_run_display_product_basin_status_idx
  ON hydro.hydro_run (basin_version_id, status)
  WHERE status IN ('succeeded', 'parsed', 'published');
