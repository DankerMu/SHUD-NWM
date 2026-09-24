-- #1729: back up, byte-exact, every row node27_1729_delete_evidence_basin.sql
-- can delete, one COPY text file per table, BEFORE the delete runs.
--
-- Run through scripts/ops/node27_oneshot_sql.py with a FRESH --copy-dir
-- (node-27: /home/nwm/tmp/1729-backup-<UTC stamp>/; files are never
-- overwritten), then keep a copy off node-27. Read-only; the runner's default
-- rollback is fine. node27_1729_delete_evidence_basin_rollback.sql restores
-- these files.
--
-- Fidelity: explicit column lists, guarded below to equal the live tables;
-- COPY text format writes geometry as hex EWKB (SRID and every coordinate
-- bit), jsonb as its canonical text, timestamptz in ISO/UTC and float8 with
-- extra_float_digits = 3; the GENERATED ALWAYS identity keys
-- (basin_version_key, river_network_version_key, station_key -- referenced
-- without FK by hydro.river_timeseries) are copied as values. The scope is
-- every row under the basin, a superset the delete asserts equals its
-- inventory.

SET LOCAL statement_timeout = '120s';
SET LOCAL search_path = pg_catalog;
SET LOCAL DateStyle = 'ISO, YMD';
SET LOCAL TimeZone = 'UTC';
SET LOCAL extra_float_digits = 3;

-- Column-set guard: every table's live columns must be exactly the explicit
-- lists the COPYs below name (a live column missing from a list would be lost
-- by the restore), and none may be a GENERATED column (COPY FROM cannot fill
-- one).
DO $$
DECLARE
    v_table text;
    v_want  text[];
    v_live  text[];
BEGIN
    FOR v_table, v_want IN SELECT * FROM (VALUES
        ('core.basin', '{basin_id,basin_name,basin_group,description,created_at}'::text[]),
        ('core.basin_version', '{basin_version_id,basin_id,version_label,geom,active_flag,valid_from,valid_to,source_uri,checksum,created_at,basin_version_key}'::text[]),
        ('core.river_network_version', '{river_network_version_id,basin_version_id,version_label,segment_count,source_uri,checksum,created_at,river_network_version_key,geometry_generation}'::text[]),
        ('core.mesh_version', '{mesh_version_id,basin_version_id,version_label,mesh_uri,checksum,properties_json,created_at}'::text[]),
        ('core.model_instance', '{model_id,basin_version_id,river_network_version_id,mesh_version_id,calibration_version_id,shud_code_version,rshud_code_version,autoshud_code_version,container_image,model_package_uri,active_flag,resource_profile,created_at,lifecycle_state}'::text[]),
        ('met.met_station', '{station_id,basin_version_id,station_name,geom,elevation_m,station_role,active_flag,properties_json,created_at,superseded_at,grid_snapshot_id,station_key}'::text[])
    ) AS expected (relation, columns)
    LOOP
        SELECT array_agg(attname::text ORDER BY attname::text COLLATE "C") INTO v_live
        FROM pg_attribute
        WHERE attrelid = v_table::regclass AND attnum > 0 AND NOT attisdropped AND attgenerated = '';
        IF v_live IS DISTINCT FROM (SELECT array_agg(c ORDER BY c COLLATE "C") FROM unnest(v_want) c) THEN
            RAISE EXCEPTION '#1729: % columns % differ from the backup column list %', v_table, v_live, v_want;
        END IF;
        IF EXISTS (SELECT 1 FROM pg_attribute WHERE attrelid = v_table::regclass AND attnum > 0
                   AND NOT attisdropped AND attgenerated <> '') THEN
            RAISE EXCEPTION '#1729: % has a GENERATED column', v_table;
        END IF;
    END LOOP;
END
$$;

-- @copy-out core.basin
COPY (
    SELECT
        basin_id, basin_name, basin_group, description, created_at
    FROM core.basin
    WHERE basin_id = 'basin__evidence_cmfd_p02_synth'
    ORDER BY basin_id
) TO STDOUT;

-- @copy-out core.basin_version
COPY (
    SELECT
        basin_version_id, basin_id, version_label, geom, active_flag, valid_from, valid_to,
        source_uri, checksum, created_at, basin_version_key
    FROM core.basin_version
    WHERE basin_id = 'basin__evidence_cmfd_p02_synth'
    ORDER BY basin_version_id
) TO STDOUT;

-- @copy-out core.river_network_version
COPY (
    SELECT
        river_network_version_id, basin_version_id, version_label, segment_count, source_uri,
        checksum, created_at, river_network_version_key, geometry_generation
    FROM core.river_network_version
    WHERE basin_version_id = 'basin__evidence_cmfd_p02_synth__v1'
    ORDER BY river_network_version_id
) TO STDOUT;

-- @copy-out core.mesh_version
COPY (
    SELECT
        mesh_version_id, basin_version_id, version_label, mesh_uri, checksum, properties_json,
        created_at
    FROM core.mesh_version
    WHERE basin_version_id = 'basin__evidence_cmfd_p02_synth__v1'
    ORDER BY mesh_version_id
) TO STDOUT;

-- @copy-out core.model_instance
COPY (
    SELECT
        model_id, basin_version_id, river_network_version_id, mesh_version_id,
        calibration_version_id, shud_code_version, rshud_code_version, autoshud_code_version,
        container_image, model_package_uri, active_flag, resource_profile, created_at,
        lifecycle_state
    FROM core.model_instance
    WHERE basin_version_id = 'basin__evidence_cmfd_p02_synth__v1'
    ORDER BY model_id
) TO STDOUT;

-- @copy-out met.met_station
COPY (
    SELECT
        station_id, basin_version_id, station_name, geom, elevation_m, station_role, active_flag,
        properties_json, created_at, superseded_at, grid_snapshot_id, station_key
    FROM met.met_station
    WHERE basin_version_id = 'basin__evidence_cmfd_p02_synth__v1'
    ORDER BY station_id
) TO STDOUT;
