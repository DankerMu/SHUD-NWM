-- feat-reach-geom-from-river-shp / PR 2: redefine the
-- ``core.river_segment_crosswalk`` UNIQUE so Path C (per-segment crosswalk)
-- can write one crosswalk row per ``gis/seg.shp`` record.
--
-- The original constraint at ``db/migrations/000004_core.sql:66`` --
-- ``UNIQUE (river_network_version_id, river_segment_id, source)`` -- allowed
-- only ONE crosswalk row per (rnv, reach_id, source). PR 2's segment-level
-- write contract attaches many segments to one reach, so every segment of the
-- same reach (e.g. ``1:3099``, ``1:3100``, ``1:3412`` all mapping to
-- ``m_reach_000001``) collided on that key and the ``ON CONFLICT DO UPDATE``
-- helper aborted with ``ON CONFLICT DO UPDATE command cannot affect row a
-- second time``.
--
-- The semantic identity is ``(rnv, source, external_id)``: ``external_id``
-- carries ``"<iRiv>:<iEle>"`` which is globally unique within ``source`` for a
-- given river-network-version. The lookup index on
-- ``(rnv, source, river_segment_id)`` (already declared in
-- ``000004_core.sql:68-69``) is preserved so the "all segments of reach X"
-- read path stays cheap; it is recreated here ``IF NOT EXISTS`` for
-- idempotence on freshly bootstrapped databases.

DO $$
DECLARE
    constraint_name TEXT;
BEGIN
    SELECT conname
    INTO constraint_name
    FROM pg_constraint
    WHERE conrelid = 'core.river_segment_crosswalk'::regclass
      AND contype = 'u'
      AND (SELECT array_agg(attname::text ORDER BY attname::text)
           FROM pg_attribute
           WHERE attrelid = conrelid AND attnum = ANY(conkey))
          = ARRAY['river_network_version_id', 'river_segment_id', 'source']::text[]
    LIMIT 1;

    IF constraint_name IS NOT NULL THEN
        EXECUTE format(
            'ALTER TABLE core.river_segment_crosswalk DROP CONSTRAINT %I',
            constraint_name
        );
    END IF;
END $$;

ALTER TABLE core.river_segment_crosswalk
    ADD CONSTRAINT river_segment_crosswalk_external_identity_uq
    UNIQUE (river_network_version_id, source, external_id);

CREATE INDEX IF NOT EXISTS river_segment_crosswalk_lookup_idx
    ON core.river_segment_crosswalk (river_network_version_id, source, river_segment_id);
