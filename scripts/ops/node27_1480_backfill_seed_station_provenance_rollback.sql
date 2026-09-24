-- #1480: undo node27_1480_backfill_seed_station_provenance.sql from the
-- properties_json backup its --apply run wrote.
--
-- Run through scripts/ops/node27_oneshot_sql.py with --copy-dir pointing at
-- that --apply run's backup directory; dry-run first, then --apply. A row is
-- restored ONLY while its current properties_json still equals exactly what
-- the backfill produced from the backed-up value, so a legitimate write made
-- after the backfill is never overwritten; such rows are counted as skipped.

SET LOCAL lock_timeout = '10s';
SET LOCAL statement_timeout = '120s';
SET LOCAL search_path = pg_catalog;

CREATE TEMP TABLE nhms_1480_backup (
    station_id text PRIMARY KEY,
    properties_json jsonb NOT NULL
) ON COMMIT DROP;

-- @copy-in met_station_properties_json
COPY pg_temp.nhms_1480_backup (station_id, properties_json) FROM STDIN;

DO $$
DECLARE
    v_backup   bigint;
    v_restored bigint;
BEGIN
    SELECT count(*) INTO v_backup FROM pg_temp.nhms_1480_backup;
    UPDATE met.met_station AS ms
    SET properties_json = b.properties_json
    FROM pg_temp.nhms_1480_backup AS b
    WHERE ms.station_id = b.station_id
      AND ms.properties_json = CASE
          WHEN jsonb_typeof(b.properties_json->'elevation_metadata') = 'object' THEN
              jsonb_set(
                  jsonb_set(b.properties_json, '{source}', to_jsonb((b.properties_json->>'project_name') || '.tsd.forc')),
                  '{elevation_metadata,source}',
                  to_jsonb((b.properties_json->>'project_name') || '.tsd.forc')
              )
          ELSE
              jsonb_set(b.properties_json, '{source}', to_jsonb((b.properties_json->>'project_name') || '.tsd.forc'))
      END;
    GET DIAGNOSTICS v_restored = ROW_COUNT;
    RAISE NOTICE '#1480 rollback restored % of % backed-up row(s); % skipped (changed since the backfill)',
        v_restored, v_backup, v_backup - v_restored;
END
$$;
