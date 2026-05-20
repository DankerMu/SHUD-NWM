CREATE TABLE IF NOT EXISTS map.tile_layer (
  layer_id TEXT PRIMARY KEY,
  layer_type TEXT NOT NULL,
  source_run_id TEXT,
  source_product_id TEXT,
  source_version TEXT,
  variable TEXT,
  valid_time TIMESTAMPTZ,
  tile_format TEXT NOT NULL,
  tile_uri_template TEXT NOT NULL,
  maplibre_source_layer TEXT,
  property_schema_version TEXT,
  property_schema_json JSONB,
  bounds_3857 DOUBLE PRECISION[],
  bounds_wgs84 DOUBLE PRECISION[],
  cache_version TEXT,
  fallback_available BOOLEAN NOT NULL DEFAULT false,
  release_blocking BOOLEAN NOT NULL DEFAULT false,
  min_zoom INT NOT NULL DEFAULT 0,
  max_zoom INT NOT NULL DEFAULT 14,
  style_json JSONB,
  published_flag BOOLEAN NOT NULL DEFAULT false,
  publish_time TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS map.tile_cache (
  layer_id TEXT NOT NULL REFERENCES map.tile_layer(layer_id),
  z INT NOT NULL,
  x INT NOT NULL,
  y INT NOT NULL,
  tile_data BYTEA,
  tile_uri TEXT,
  cache_key TEXT,
  etag TEXT,
  checksum TEXT,
  source_id TEXT,
  source_version TEXT,
  valid_time TIMESTAMPTZ,
  style_id TEXT NOT NULL DEFAULT 'default',
  schema_version TEXT,
  encoder_version TEXT,
  status TEXT NOT NULL DEFAULT 'ready',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (layer_id, z, x, y)
);

CREATE UNIQUE INDEX IF NOT EXISTS tile_cache_key_idx ON map.tile_cache (cache_key) WHERE cache_key IS NOT NULL;
