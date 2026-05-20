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
SET cache_key = NULL
WHERE cache_key IS NOT NULL
  AND btrim(cache_key) = '';

UPDATE map.tile_cache
SET tile_uri = NULL
WHERE tile_uri IS NOT NULL
  AND btrim(tile_uri) = '';

UPDATE map.tile_cache
SET cache_key = tile_uri
WHERE cache_key IS NULL
  AND tile_uri IS NOT NULL;

UPDATE map.tile_cache
SET cache_key = encode(
  digest(
    jsonb_build_object(
      'legacy_identity', 'map.tile_cache',
      'layer_id', layer_id,
      'z', z,
      'x', x,
      'y', y,
      'source_id', source_id,
      'source_version', source_version,
      'valid_time', CASE
        WHEN valid_time IS NULL THEN NULL
        ELSE to_char(valid_time AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"')
      END,
      'style_id', COALESCE(style_id, 'default'),
      'schema_version', schema_version,
      'encoder_version', encoder_version
    )::text,
    'sha256'
  ),
  'hex'
)
WHERE cache_key IS NULL;

DO $$
DECLARE
  duplicate_count BIGINT;
BEGIN
  SELECT COUNT(*)
    INTO duplicate_count
    FROM (
      SELECT cache_key
      FROM map.tile_cache
      GROUP BY cache_key
      HAVING COUNT(*) > 1
    ) duplicate_cache_keys;

  IF duplicate_count > 0 THEN
    RAISE EXCEPTION
      'Duplicate tile cache cache_key rows exist after deterministic M16 backfill. Deduplicate or quarantine duplicate cache rows before applying migration 000018.';
  END IF;
END $$;

ALTER TABLE map.tile_cache
  ALTER COLUMN cache_key SET NOT NULL;

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
