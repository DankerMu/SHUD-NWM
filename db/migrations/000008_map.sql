CREATE TABLE IF NOT EXISTS map.tile_layer (
  layer_id TEXT PRIMARY KEY,
  layer_type TEXT NOT NULL,
  source_run_id TEXT,
  source_product_id TEXT,
  variable TEXT,
  valid_time TIMESTAMPTZ,
  tile_format TEXT NOT NULL,
  tile_uri_template TEXT NOT NULL,
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
  etag TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (layer_id, z, x, y)
);
