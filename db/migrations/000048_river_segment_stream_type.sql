-- Persist the SHUD river hierarchy used by low-zoom national MVT queries.
-- The generated column keeps every existing and future importer consistent:
-- importers continue to own properties_json, PostgreSQL owns the query column.
ALTER TABLE core.river_segment
  ADD COLUMN IF NOT EXISTS stream_type DOUBLE PRECISION
  GENERATED ALWAYS AS (
    CASE
      WHEN (properties_json ->> 'Type') ~ '^[0-9]+([.][0-9]+)?$'
      THEN LEAST(
        5.0::double precision,
        GREATEST(1.0::double precision, (properties_json ->> 'Type')::double precision)
      )
      ELSE NULL
    END
  ) STORED;

CREATE INDEX IF NOT EXISTS river_segment_network_stream_type_idx
  ON core.river_segment (
    river_network_version_id,
    stream_type DESC,
    river_segment_id
  );

