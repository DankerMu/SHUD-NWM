ALTER TABLE map.tile_layer
  ADD COLUMN IF NOT EXISTS source_run_id TEXT,
  ADD COLUMN IF NOT EXISTS source_product_id TEXT,
  ADD COLUMN IF NOT EXISTS source_version TEXT,
  ADD COLUMN IF NOT EXISTS variable TEXT,
  ADD COLUMN IF NOT EXISTS valid_time TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS maplibre_source_layer TEXT,
  ADD COLUMN IF NOT EXISTS property_schema_version TEXT,
  ADD COLUMN IF NOT EXISTS property_schema_json JSONB,
  ADD COLUMN IF NOT EXISTS bounds_3857 DOUBLE PRECISION[],
  ADD COLUMN IF NOT EXISTS bounds_wgs84 DOUBLE PRECISION[],
  ADD COLUMN IF NOT EXISTS cache_version TEXT,
  ADD COLUMN IF NOT EXISTS fallback_available BOOLEAN NOT NULL DEFAULT false,
  ADD COLUMN IF NOT EXISTS release_blocking BOOLEAN NOT NULL DEFAULT false,
  ADD COLUMN IF NOT EXISTS min_zoom INT NOT NULL DEFAULT 0,
  ADD COLUMN IF NOT EXISTS max_zoom INT NOT NULL DEFAULT 14,
  ADD COLUMN IF NOT EXISTS style_json JSONB,
  ADD COLUMN IF NOT EXISTS published_flag BOOLEAN NOT NULL DEFAULT false,
  ADD COLUMN IF NOT EXISTS publish_time TIMESTAMPTZ;

ALTER TABLE map.tile_cache
  ADD COLUMN IF NOT EXISTS cache_key TEXT,
  ADD COLUMN IF NOT EXISTS etag TEXT,
  ADD COLUMN IF NOT EXISTS checksum TEXT,
  ADD COLUMN IF NOT EXISTS source_id TEXT,
  ADD COLUMN IF NOT EXISTS source_version TEXT,
  ADD COLUMN IF NOT EXISTS valid_time TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS style_id TEXT NOT NULL DEFAULT 'default',
  ADD COLUMN IF NOT EXISTS schema_version TEXT,
  ADD COLUMN IF NOT EXISTS encoder_version TEXT,
  ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'ready';

UPDATE map.tile_cache
SET cache_key = COALESCE(cache_key, tile_uri)
WHERE cache_key IS NULL
  AND tile_uri IS NOT NULL;

DO $$
DECLARE
  pk_name TEXT;
  pk_columns TEXT;
BEGIN
  SELECT con.conname,
         string_agg(att.attname, ',' ORDER BY key_column.ordinality)
    INTO pk_name, pk_columns
    FROM pg_constraint con
    JOIN unnest(con.conkey) WITH ORDINALITY AS key_column(attnum, ordinality) ON true
    JOIN pg_attribute att ON true
   WHERE con.conrelid = 'map.tile_cache'::regclass
     AND con.contype = 'p'
     AND att.attrelid = con.conrelid
     AND att.attnum = key_column.attnum
   GROUP BY con.conname;

  IF pk_name IS NOT NULL AND pk_columns <> 'cache_key' THEN
    EXECUTE format('ALTER TABLE map.tile_cache DROP CONSTRAINT %I', pk_name);
  END IF;
END $$;

CREATE UNIQUE INDEX IF NOT EXISTS tile_cache_cache_key_uidx ON map.tile_cache (cache_key);
CREATE INDEX IF NOT EXISTS tile_cache_xyz_idx ON map.tile_cache (layer_id, z, x, y);
