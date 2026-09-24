-- #1729: restore the rows node27_1729_delete_evidence_basin.sql deleted, from
-- the files node27_1729_delete_evidence_basin_backup.sql wrote.
--
-- Run through scripts/ops/node27_oneshot_sql.py with --copy-dir pointing at
-- that backup directory; dry-run first (rolled back), then --apply. Parents
-- first (the delete's reverse FK order). COPY FROM keeps the supplied values
-- of the GENERATED ALWAYS identity keys -- the byte-exact counterpart of
-- INSERT ... OVERRIDING SYSTEM VALUE, proven by
-- tests/test_node27_1729_evidence_basin_delete_integration.py -- so
-- hydro.river_timeseries' FK-less *_key references stay valid. A row that
-- still (or again) exists fails its primary key and rolls the whole restore
-- back.

SET LOCAL lock_timeout = '10s';
SET LOCAL statement_timeout = '300s';
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

-- @copy-in core.basin
COPY core.basin (
    basin_id, basin_name, basin_group, description, created_at
) FROM STDIN;

-- @copy-in core.basin_version
COPY core.basin_version (
    basin_version_id, basin_id, version_label, geom, active_flag, valid_from, valid_to, source_uri,
    checksum, created_at, basin_version_key
) FROM STDIN;

-- @copy-in core.river_network_version
COPY core.river_network_version (
    river_network_version_id, basin_version_id, version_label, segment_count, source_uri, checksum,
    created_at, river_network_version_key, geometry_generation
) FROM STDIN;

-- @copy-in core.mesh_version
COPY core.mesh_version (
    mesh_version_id, basin_version_id, version_label, mesh_uri, checksum, properties_json,
    created_at
) FROM STDIN;

-- @copy-in core.model_instance
COPY core.model_instance (
    model_id, basin_version_id, river_network_version_id, mesh_version_id, calibration_version_id,
    shud_code_version, rshud_code_version, autoshud_code_version, container_image,
    model_package_uri, active_flag, resource_profile, created_at, lifecycle_state
) FROM STDIN;

-- @copy-in met.met_station
COPY met.met_station (
    station_id, basin_version_id, station_name, geom, elevation_m, station_role, active_flag,
    properties_json, created_at, superseded_at, grid_snapshot_id, station_key
) FROM STDIN;

-- Every deleted row is back, ids exactly as inventoried.
DO $$
DECLARE
    v_n bigint;
BEGIN
    SELECT count(*) INTO v_n FROM core.basin
    WHERE basin_id = 'basin__evidence_cmfd_p02_synth' AND basin_group = 'evidence-only';
    IF v_n <> 1 THEN RAISE EXCEPTION '#1729 rollback: core.basin=% (want 1)', v_n; END IF;
    SELECT count(*) INTO v_n FROM core.basin_version WHERE basin_id = 'basin__evidence_cmfd_p02_synth';
    IF v_n <> 1 THEN RAISE EXCEPTION '#1729 rollback: core.basin_version=% (want 1)', v_n; END IF;
    SELECT count(*) INTO v_n FROM core.river_network_version
    WHERE basin_version_id = 'basin__evidence_cmfd_p02_synth__v1';
    IF v_n <> 1 THEN RAISE EXCEPTION '#1729 rollback: core.river_network_version=% (want 1)', v_n; END IF;
    SELECT count(*) INTO v_n FROM core.mesh_version WHERE basin_version_id = 'basin__evidence_cmfd_p02_synth__v1';
    IF v_n <> 1 THEN RAISE EXCEPTION '#1729 rollback: core.mesh_version=% (want 1)', v_n; END IF;
    SELECT count(*) INTO v_n FROM core.model_instance WHERE basin_version_id = 'basin__evidence_cmfd_p02_synth__v1';
    IF v_n <> 2 THEN RAISE EXCEPTION '#1729 rollback: core.model_instance=% (want 2)', v_n; END IF;
    SELECT count(*) INTO v_n FROM met.met_station WHERE basin_version_id = 'basin__evidence_cmfd_p02_synth__v1';
    IF v_n <> 6 THEN RAISE EXCEPTION '#1729 rollback: met.met_station=% (want 6)', v_n; END IF;
    RAISE NOTICE '#1729 rollback restored basin=1 basin_version=1 river_network_version=1 mesh_version=1 model_instance=2 met_station=6';
END
$$;
