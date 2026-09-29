-- @settings
--
-- #2621: restore the rows node27_2621_delete_rename_leftovers.sql deleted, from
-- the eight NAME.copy files that delete wrote in its own --apply transaction.
--
-- Run through scripts/ops/node27_oneshot_sql.py with --copy-dir pointing at the
-- --apply delete run's directory (node-27:
-- /home/nwm/tmp/2621-delete-apply-<ts>/ or its off-node copy) -- NOT the
-- dry-run's directory. This file reads no --set key (@settings above, so the
-- runner refuses any). Dry-run first (rolled back), then --apply.
--
-- Parents first (the delete's reverse FK order), every RI check on: nothing is
-- restored in replica mode. COPY FROM keeps the supplied values of the
-- GENERATED ALWAYS identity keys (basin_version_key, river_network_version_key,
-- station_key, river_segment_key) -- the byte-exact counterpart of INSERT ...
-- OVERRIDING SYSTEM VALUE -- and of the serial crosswalk_id, so
-- hydro.river_timeseries' FK-less *_key references stay valid. The generated
-- river_segment.stream_type is left out of the column lists and recomputed
-- from properties_json. Proven byte-exact by
-- tests/test_node27_2621_rename_leftovers_delete_integration.py.
--
-- Refuses to restore over rows that still (or again) exist: the precheck below
-- RAISEs before any COPY, and a primary key would fail the COPY anyway.

SET LOCAL lock_timeout = '10s';
SET LOCAL statement_timeout = '300s';
SET LOCAL search_path = pg_catalog;
SET LOCAL DateStyle = 'ISO, YMD';
SET LOCAL TimeZone = 'UTC';
SET LOCAL extra_float_digits = 3;

DO $$
DECLARE
    c_basins constant text[] := ARRAY[
        'basins_dnzh_mdzh', 'basins_dnzh_mj', 'basins_dnzh_mnzh', 'basins_dnzh_qtj',
        'basins_xinan_dulongjiang', 'basins_xinan_lancangjiang', 'basins_xinan_nujiang'
    ];
    v_bvs    text[] := ARRAY(SELECT b || '_vbasins' FROM unnest(c_basins) b);
    v_rnvs   text[] := ARRAY(SELECT b || '_rivnet_vbasins' FROM unnest(c_basins) b);
    v_meshes text[] := ARRAY(SELECT b || '_mesh_vbasins' FROM unnest(c_basins) b);
    v_found  text := '';
    v_n      bigint;
BEGIN
    SELECT count(*) INTO v_n FROM core.basin WHERE basin_id = ANY (c_basins);
    IF v_n <> 0 THEN v_found := v_found || ' core.basin=' || v_n; END IF;
    SELECT count(*) INTO v_n FROM core.basin_version WHERE basin_version_id = ANY (v_bvs);
    IF v_n <> 0 THEN v_found := v_found || ' core.basin_version=' || v_n; END IF;
    SELECT count(*) INTO v_n FROM core.river_network_version WHERE river_network_version_id = ANY (v_rnvs);
    IF v_n <> 0 THEN v_found := v_found || ' core.river_network_version=' || v_n; END IF;
    SELECT count(*) INTO v_n FROM core.mesh_version WHERE mesh_version_id = ANY (v_meshes);
    IF v_n <> 0 THEN v_found := v_found || ' core.mesh_version=' || v_n; END IF;
    SELECT count(*) INTO v_n FROM core.model_instance
    WHERE basin_version_id = ANY (v_bvs) OR river_network_version_id = ANY (v_rnvs) OR mesh_version_id = ANY (v_meshes);
    IF v_n <> 0 THEN v_found := v_found || ' core.model_instance=' || v_n; END IF;
    SELECT count(*) INTO v_n FROM met.met_station WHERE basin_version_id = ANY (v_bvs);
    IF v_n <> 0 THEN v_found := v_found || ' met.met_station=' || v_n; END IF;
    SELECT count(*) INTO v_n FROM core.river_segment WHERE river_network_version_id = ANY (v_rnvs);
    IF v_n <> 0 THEN v_found := v_found || ' core.river_segment=' || v_n; END IF;
    SELECT count(*) INTO v_n FROM core.river_segment_crosswalk WHERE river_network_version_id = ANY (v_rnvs);
    IF v_n <> 0 THEN v_found := v_found || ' core.river_segment_crosswalk=' || v_n; END IF;
    IF v_found <> '' THEN
        RAISE EXCEPTION '#2621 rollback: target rows still exist (%); refusing to restore over them', btrim(v_found);
    END IF;
END
$$;

-- Column-set guard: every table's live columns must be exactly the explicit
-- lists the COPYs below name (a live column missing from a list would be lost
-- by the restore), and its GENERATED columns exactly the pinned ones, which
-- the COPYs leave out and COPY FROM recomputes.
DO $$
DECLARE
    v_table     text;
    v_want      text[];
    v_generated text[];
    v_live      text[];
BEGIN
    FOR v_table, v_want, v_generated IN SELECT * FROM (VALUES
        ('core.basin', '{basin_id,basin_name,basin_group,description,created_at}'::text[], '{}'::text[]),
        ('core.basin_version', '{basin_version_id,basin_id,version_label,geom,active_flag,valid_from,valid_to,source_uri,checksum,created_at,basin_version_key}'::text[], '{}'::text[]),
        ('core.river_network_version', '{river_network_version_id,basin_version_id,version_label,segment_count,source_uri,checksum,created_at,river_network_version_key,geometry_generation}'::text[], '{}'::text[]),
        ('core.mesh_version', '{mesh_version_id,basin_version_id,version_label,mesh_uri,checksum,properties_json,created_at}'::text[], '{}'::text[]),
        ('core.model_instance', '{model_id,basin_version_id,river_network_version_id,mesh_version_id,calibration_version_id,shud_code_version,rshud_code_version,autoshud_code_version,container_image,model_package_uri,active_flag,resource_profile,created_at,lifecycle_state}'::text[], '{}'::text[]),
        ('met.met_station', '{station_id,basin_version_id,station_name,geom,elevation_m,station_role,active_flag,properties_json,created_at,superseded_at,grid_snapshot_id,station_key}'::text[], '{}'::text[]),
        ('core.river_segment', '{river_segment_id,river_network_version_id,segment_order,downstream_segment_id,length_m,geom,properties_json,created_at,river_segment_key}'::text[], '{stream_type}'::text[]),
        ('core.river_segment_crosswalk', '{crosswalk_id,river_network_version_id,river_segment_id,source,external_id,properties_json,created_at}'::text[], '{}'::text[])
    ) AS expected (relation, columns, generated)
    LOOP
        SELECT array_agg(attname::text ORDER BY attname::text COLLATE "C") INTO v_live
        FROM pg_attribute
        WHERE attrelid = v_table::regclass AND attnum > 0 AND NOT attisdropped AND attgenerated = '';
        IF v_live IS DISTINCT FROM (SELECT array_agg(c ORDER BY c COLLATE "C") FROM unnest(v_want) c) THEN
            RAISE EXCEPTION '#2621: % columns % differ from the backup column list %', v_table, v_live, v_want;
        END IF;
        SELECT coalesce(array_agg(attname::text ORDER BY attname::text COLLATE "C"), '{}') INTO v_live
        FROM pg_attribute
        WHERE attrelid = v_table::regclass AND attnum > 0 AND NOT attisdropped AND attgenerated <> '';
        IF v_live IS DISTINCT FROM v_generated THEN
            RAISE EXCEPTION '#2621: % GENERATED columns % (want %)', v_table, v_live, v_generated;
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

-- @copy-in core.river_segment
COPY core.river_segment (
    river_segment_id, river_network_version_id, segment_order, downstream_segment_id, length_m,
    geom, properties_json, created_at, river_segment_key
) FROM STDIN;

-- @copy-in core.river_segment_crosswalk
COPY core.river_segment_crosswalk (
    crosswalk_id, river_network_version_id, river_segment_id, source, external_id,
    properties_json, created_at
) FROM STDIN;

-- Every deleted row is back, in the inventoried counts.
DO $$
DECLARE
    c_basins constant text[] := ARRAY[
        'basins_dnzh_mdzh', 'basins_dnzh_mj', 'basins_dnzh_mnzh', 'basins_dnzh_qtj',
        'basins_xinan_dulongjiang', 'basins_xinan_lancangjiang', 'basins_xinan_nujiang'
    ];
    v_bvs    text[] := ARRAY(SELECT b || '_vbasins' FROM unnest(c_basins) b);
    v_rnvs   text[] := ARRAY(SELECT b || '_rivnet_vbasins' FROM unnest(c_basins) b);
    v_counts bigint[];
BEGIN
    SELECT ARRAY[
        (SELECT count(*) FROM core.basin WHERE basin_id = ANY (c_basins)),
        (SELECT count(*) FROM core.basin_version WHERE basin_version_id = ANY (v_bvs)),
        (SELECT count(*) FROM core.river_network_version WHERE basin_version_id = ANY (v_bvs)),
        (SELECT count(*) FROM core.mesh_version WHERE basin_version_id = ANY (v_bvs)),
        (SELECT count(*) FROM core.model_instance WHERE basin_version_id = ANY (v_bvs)),
        (SELECT count(*) FROM met.met_station WHERE basin_version_id = ANY (v_bvs)),
        (SELECT count(*) FROM core.river_segment WHERE river_network_version_id = ANY (v_rnvs)),
        (SELECT count(*) FROM core.river_segment_crosswalk WHERE river_network_version_id = ANY (v_rnvs))
    ] INTO v_counts;
    IF v_counts IS DISTINCT FROM ARRAY[7, 7, 7, 7, 21, 2290, 85196, 85471]::bigint[] THEN
        RAISE EXCEPTION '#2621 rollback: restored % (want {7,7,7,7,21,2290,85196,85471})', v_counts;
    END IF;
    RAISE NOTICE '#2621 rollback restored basin=7 basin_version=7 river_network_version=7 mesh_version=7 '
        'model_instance=21 met_station=2290 river_segment=85196 river_segment_crosswalk=85471';
END
$$;
