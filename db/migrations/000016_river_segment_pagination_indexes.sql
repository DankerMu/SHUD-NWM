CREATE INDEX IF NOT EXISTS river_segment_network_order_idx
  ON core.river_segment (river_network_version_id, segment_order, river_segment_id);

CREATE INDEX IF NOT EXISTS river_network_version_basin_lookup_idx
  ON core.river_network_version (basin_version_id, river_network_version_id);
