-- Widen core.river_segment.geom from LineString to MultiLineString so a reach can
-- express a real source gap as separate parts instead of a fabricated cross-gap
-- straight bridge (root fix for the qhh/heihe "跨缝直线跳变"; see
-- workers/model_registry/basins_geometry.gap_split_multilinestring_wkt and the
-- frontend splitPositionsAtGaps). This migration only changes the column TYPE:
-- existing LineStrings are wrapped into single-part MultiLineStrings via ST_Multi
-- (NULL stays NULL). The per-row gap SPLIT is done by
-- scripts/backfill_river_segment_multilinestring.py (or a re-import), which is a
-- data UPDATE, not a schema change.
--
-- Idempotent / re-entrant: a DO guard checks the live column type via
-- geometry_columns and skips the ALTER once geom is already MultiLineString, so
-- re-running the migration set never errors.

DO $$
DECLARE
    current_type text;
BEGIN
    SELECT type
      INTO current_type
      FROM geometry_columns
     WHERE f_table_schema = 'core'
       AND f_table_name = 'river_segment'
       AND f_geometry_column = 'geom';

    IF current_type IS NULL THEN
        RAISE EXCEPTION 'core.river_segment.geom is not a registered geometry column';
    END IF;

    IF current_type <> 'MULTILINESTRING' THEN
        ALTER TABLE core.river_segment
            ALTER COLUMN geom TYPE geometry(MultiLineString, 4490)
            USING ST_Multi(geom);
    END IF;
END
$$;

-- The GiST index is geometry-type agnostic; recreate it defensively in case an
-- older deployment lacks it (the type change above does not drop it).
CREATE INDEX IF NOT EXISTS river_segment_geom_gix
  ON core.river_segment USING gist (geom);
