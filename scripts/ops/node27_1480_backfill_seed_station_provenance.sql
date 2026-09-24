-- #1480: correct the self-contradictory provenance of met.met_station seed
-- rows written before #1415: properties_json.source and
-- properties_json.elevation_metadata.source were hard-coded 'qhh.tsd.forc'
-- while project_name (and forcing_source_identity / source_file) name the real
-- project (node-27: 1709 basins_heihe_vbasins rows, project_name = 'heihe').
--
-- Run ONLY through scripts/ops/node27_oneshot_sql.py with a FRESH --copy-dir
-- (node-27: /home/nwm/tmp/1480-backup-<UTC stamp>/), and ONLY after the user
-- confirmed the dry-run (tasks.md 6.3). Dry-run is the runner default (rolled
-- back); --apply commits. The dry-run's backup file is the dry-run's alone --
-- the --apply run needs its own fresh --copy-dir, whose file is what
-- node27_1480_backfill_seed_station_provenance_rollback.sql restores.
--
-- What changes, and nothing else:
--   * rows: seed = 'qhh_production_bootstrap' with a non-NULL project_name
--     whose source, or (only when elevation_metadata is a JSON object) whose
--     elevation_metadata.source, differs from '<project_name>.tsd.forc';
--   * keys: exactly source and elevation_metadata.source, set to
--     '<project_name>.tsd.forc' from the SAME row (never a hard-coded basin).
--     A missing / non-object elevation_metadata is never created, so such a
--     row is not matched again: a second run touches 0 rows.
-- The UPDATE must touch exactly nhms_1480.expected_rows rows (default 1709,
-- the node-27 dry-run count; the idempotency re-run passes
-- --set nhms_1480.expected_rows=0), else it RAISEs and rolls back.

SET LOCAL lock_timeout = '10s';
SET LOCAL statement_timeout = '120s';
SET LOCAL search_path = pg_catalog;

-- Lock the target rows first, so the backup below and the UPDATE after it
-- see one row set. FOR NO KEY UPDATE, not FOR UPDATE: only properties_json
-- changes, never a key, so the FK inserts that take FOR KEY SHARE on these
-- station rows (met.interp_weight, met.forcing_station_timeseries ingest) are
-- neither blocked by nor block the backfill.
SELECT count(*) AS locked_rows
FROM (
    SELECT 1
    FROM met.met_station
    WHERE properties_json->>'seed' = 'qhh_production_bootstrap'
      AND properties_json->>'project_name' IS NOT NULL
      AND (
          properties_json->>'source' IS DISTINCT FROM (properties_json->>'project_name') || '.tsd.forc'
          OR (
              jsonb_typeof(properties_json->'elevation_metadata') = 'object'
              AND properties_json#>>'{elevation_metadata,source}'
                  IS DISTINCT FROM (properties_json->>'project_name') || '.tsd.forc'
          )
      )
    FOR NO KEY UPDATE
) AS target;

-- Full original properties_json of every row about to change, by station_id.
-- @copy-out met_station_properties_json
COPY (
    SELECT station_id, properties_json
    FROM met.met_station
    WHERE properties_json->>'seed' = 'qhh_production_bootstrap'
      AND properties_json->>'project_name' IS NOT NULL
      AND (
          properties_json->>'source' IS DISTINCT FROM (properties_json->>'project_name') || '.tsd.forc'
          OR (
              jsonb_typeof(properties_json->'elevation_metadata') = 'object'
              AND properties_json#>>'{elevation_metadata,source}'
                  IS DISTINCT FROM (properties_json->>'project_name') || '.tsd.forc'
          )
      )
    ORDER BY station_id
) TO STDOUT;

DO $$
DECLARE
    v_expected bigint := coalesce(nullif(current_setting('nhms_1480.expected_rows', true), ''), '1709')::bigint;
    v_n        bigint;
    v_group    record;
BEGIN
    -- Receipt: the pre-update provenance groups of every seed row (same
    -- grouping as the post-update receipt below, so one dry-run is the whole
    -- before/after picture).
    FOR v_group IN
        SELECT properties_json->>'project_name' AS project_name,
               properties_json->>'source' AS source,
               properties_json#>>'{elevation_metadata,source}' AS elevation_source,
               count(*) AS n
        FROM met.met_station
        WHERE properties_json->>'seed' = 'qhh_production_bootstrap'
        GROUP BY 1, 2, 3
        ORDER BY 1, 2, 3
    LOOP
        RAISE NOTICE '#1480 before: project_name=% source=% elevation_source=% count=%',
            v_group.project_name, v_group.source, v_group.elevation_source, v_group.n;
    END LOOP;
    UPDATE met.met_station
    SET properties_json = CASE
        WHEN jsonb_typeof(properties_json->'elevation_metadata') = 'object' THEN
            jsonb_set(
                jsonb_set(properties_json, '{source}', to_jsonb((properties_json->>'project_name') || '.tsd.forc')),
                '{elevation_metadata,source}',
                to_jsonb((properties_json->>'project_name') || '.tsd.forc')
            )
        ELSE
            jsonb_set(properties_json, '{source}', to_jsonb((properties_json->>'project_name') || '.tsd.forc'))
    END
    WHERE properties_json->>'seed' = 'qhh_production_bootstrap'
      AND properties_json->>'project_name' IS NOT NULL
      AND (
          properties_json->>'source' IS DISTINCT FROM (properties_json->>'project_name') || '.tsd.forc'
          OR (
              jsonb_typeof(properties_json->'elevation_metadata') = 'object'
              AND properties_json#>>'{elevation_metadata,source}'
                  IS DISTINCT FROM (properties_json->>'project_name') || '.tsd.forc'
          )
      );
    GET DIAGNOSTICS v_n = ROW_COUNT;
    IF v_n <> v_expected THEN
        RAISE EXCEPTION '#1480: backfill touched % row(s), expected % (nhms_1480.expected_rows)', v_n, v_expected;
    END IF;
    RAISE NOTICE '#1480 backfilled rows: %', v_n;
    -- Receipt: the post-update provenance groups of every seed row.
    FOR v_group IN
        SELECT properties_json->>'project_name' AS project_name,
               properties_json->>'source' AS source,
               properties_json#>>'{elevation_metadata,source}' AS elevation_source,
               count(*) AS n
        FROM met.met_station
        WHERE properties_json->>'seed' = 'qhh_production_bootstrap'
        GROUP BY 1, 2, 3
        ORDER BY 1, 2, 3
    LOOP
        RAISE NOTICE '#1480 after: project_name=% source=% elevation_source=% count=%',
            v_group.project_name, v_group.source, v_group.elevation_source, v_group.n;
    END LOOP;
END
$$;
