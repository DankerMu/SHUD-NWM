-- #1729: delete the synthetic evidence basin basin__evidence_cmfd_p02_synth
-- (basin_group = 'evidence-only') and the rows under it from the node-27
-- primary, in FK order, in ONE transaction that first locks and backs up
-- exactly the rows it then deletes.
--
-- Run ONLY through scripts/ops/node27_oneshot_sql.py, with a FRESH --copy-dir
-- per run (node-27: /home/nwm/tmp/1729-delete-<UTC stamp>/; the runner refuses
-- this file without one and never overwrites a copy file), and ONLY after the
-- user confirmed the dry-run (tasks.md 6.3). Operator steps:
--   1. dry-run (the runner default: rolled back; NOTICEs carry every count and
--      per-step timing) with --copy-dir /home/nwm/tmp/1729-delete-dry-<ts>/.
--      Its files are the dry-run's alone: NEVER reuse that directory for
--      step 2 (the runner fails on the first existing file anyway);
--   2. --apply with a new --copy-dir /home/nwm/tmp/1729-delete-apply-<ts>/;
--      the six NAME.copy files it writes are the backup of exactly the rows
--      it deleted, fsynced before COMMIT. Copy that directory off node-27;
--   3. restore, if ever needed: node27_1729_delete_evidence_basin_rollback.sql
--      with --copy-dir pointing at the step-2 directory.
-- node27_1729_delete_evidence_basin_backup.sql is an optional read-only
-- precheck only; its files are NOT the restore source.
--
-- Fail-closed: any deviation below RAISEs, which rolls back every delete.
--   * the basin must exist with basin_group = 'evidence-only';
--   * the live FK set whose parent is one of the seven tables the delete
--     touches or cascades through must equal the 20 non-chunk FKs inventoried
--     on node-27 (.workplans/k3/k3-catalog.out). TimescaleDB per-chunk copies
--     (conrelid in _timescaledb_internal) are excluded: a new chunk every day
--     would otherwise grow the set. An unknown dependent table fails here
--     before anything is deleted;
--   * no user trigger on the six tables deleted from;
--   * the id sets under the basin must be exactly the inventoried ones, and
--     every non-hypertable FK dependent must hold 0 rows for them;
--   * the rows about to be deleted are then locked FOR UPDATE, parents first
--     (basin -> basin_version -> river_network_version -> mesh_version ->
--     model_instance -> met_station; the #2491 basin_version ->
--     river_network_version order), each lock asserting the inventory count.
--     A locked parent also blocks any new FK child row (its FOR KEY SHARE
--     conflicts), so the backup COPYs -- same predicates as the DELETEs --
--     write exactly the rows the DELETEs then remove; the column-set guard is
--     byte-identical to the backup/rollback scripts' (pinned by
--     tests/test_node27_oneshot_sql.py).
-- Hypertables are not scanned explicitly (.workplans/k3/PROBE-NOTE.txt: a
-- scan of their station/key columns ran >10 min on the live primary), but
-- they are not free: each of the 6 met.met_station deletes fires the NO ACTION
-- RI lookups of met.forcing_station_timeseries(station_key) and
-- met.forcing_station_timeseries_legacy(station_id) on EVERY chunk, including
-- the compressed-chunk FK copies, through non-leading index columns. That cost
-- is bounded by the statement_timeout below; pause the compression/retention
-- jobs for the run and watch pg_stat_activity (application_name
-- nhms-oneshot-sql) during the dry-run. A statement_timeout there is reported
-- and the run stops; it is not retried with a larger timeout (design D3).
--   * those two FKs are the guard -- a referencing row makes the DELETE fail
--     and rolls everything back;
--   * hydro.river_timeseries.basin_version_key / river_network_version_key
--     have no FK, but its run_key is an FK to hydro.hydro_run (000059) and the
--     basin has 0 hydro_run rows (asserted below); the legacy table's
--     basin_version_id rides the NOT NULL forcing_version_id FK and the models
--     have 0 met.forcing_version rows (asserted below). So neither can hold a
--     row for this basin.
-- Inventoried, never deleted (append-only / no FK): ops.audit_log (equality
-- count only, never LIKE), ops.pipeline_job.model_id,
-- hydro.state_snapshot.cloned_from_model_id and flood.return_period_result are
-- reported below as NOTICEs; met.canonical_grid_*, ops.pipeline_event and
-- ops.qc_result carry no FK to these rows and are listed in the receipt only,
-- not scanned here.

SET LOCAL lock_timeout = '10s';
SET LOCAL statement_timeout = '300s';
SET LOCAL search_path = pg_catalog;
-- The backup's text format (same settings as the backup/rollback scripts).
SET LOCAL DateStyle = 'ISO, YMD';
SET LOCAL TimeZone = 'UTC';
SET LOCAL extra_float_digits = 3;

DO $$
DECLARE
    c_basin    constant text   := 'basin__evidence_cmfd_p02_synth';
    c_bv       constant text   := 'basin__evidence_cmfd_p02_synth__v1';
    c_rnv      constant text   := 'rnw__evidence_cmfd_p02_synth__v1';
    c_mesh     constant text   := 'mesh__evidence_cmfd_p02_synth__v1';
    c_models   constant text[] := ARRAY[
        'dg_10d27a62b35b39cb5a6f9d10f7fff6e9',
        'model__evidence_cmfd_p02_synth__v1'
    ];
    c_stations constant text[] := ARRAY[
        'synth-mip-m1-v2::cell:cell-0100.00-0030.00',
        'synth-mip-m1-v2::cell:cell-0100.00-0030.50',
        'synth-mip-m1-v2::cell:cell-0100.50-0030.00',
        'synth-station-001',
        'synth-station-002',
        'synth-station-003'
    ];
    -- child | parent | pg_get_constraintdef, sorted (node-27 k3-catalog.out).
    c_expected_fks constant text[] := ARRAY[
        'core.basin_version|core.basin|FOREIGN KEY (basin_id) REFERENCES core.basin(basin_id)',
        'core.mesh_version|core.basin_version|FOREIGN KEY (basin_version_id) REFERENCES core.basin_version(basin_version_id)',
        'core.model_instance|core.basin_version|FOREIGN KEY (basin_version_id) REFERENCES core.basin_version(basin_version_id)',
        'core.model_instance|core.river_network_version|FOREIGN KEY (river_network_version_id) REFERENCES core.river_network_version(river_network_version_id)',
        'core.river_network_version|core.basin_version|FOREIGN KEY (basin_version_id) REFERENCES core.basin_version(basin_version_id)',
        'core.river_segment|core.river_network_version|FOREIGN KEY (river_network_version_id) REFERENCES core.river_network_version(river_network_version_id)',
        'flood.flood_frequency_curve|core.model_instance|FOREIGN KEY (model_id) REFERENCES core.model_instance(model_id)',
        'hydro.hydro_run|core.basin_version|FOREIGN KEY (basin_version_id) REFERENCES core.basin_version(basin_version_id)',
        'hydro.hydro_run|core.model_instance|FOREIGN KEY (model_id) REFERENCES core.model_instance(model_id)',
        'hydro.hydro_run|met.forcing_version|FOREIGN KEY (forcing_version_id) REFERENCES met.forcing_version(forcing_version_id)',
        'hydro.state_snapshot|core.model_instance|FOREIGN KEY (model_id) REFERENCES core.model_instance(model_id)',
        'met.forcing_station_timeseries_legacy|met.forcing_version|FOREIGN KEY (forcing_version_id) REFERENCES met.forcing_version(forcing_version_id)',
        'met.forcing_station_timeseries_legacy|met.met_station|FOREIGN KEY (station_id) REFERENCES met.met_station(station_id)',
        'met.forcing_station_timeseries|met.forcing_version|FOREIGN KEY (forcing_version_key) REFERENCES met.forcing_version(forcing_version_key)',
        'met.forcing_station_timeseries|met.met_station|FOREIGN KEY (station_key) REFERENCES met.met_station(station_key)',
        'met.forcing_version_component|met.forcing_version|FOREIGN KEY (forcing_version_id) REFERENCES met.forcing_version(forcing_version_id)',
        'met.forcing_version|core.model_instance|FOREIGN KEY (model_id) REFERENCES core.model_instance(model_id)',
        'met.interp_weight|core.model_instance|FOREIGN KEY (model_id) REFERENCES core.model_instance(model_id)',
        'met.interp_weight|met.met_station|FOREIGN KEY (station_id) REFERENCES met.met_station(station_id)',
        'met.met_station|core.basin_version|FOREIGN KEY (basin_version_id) REFERENCES core.basin_version(basin_version_id)'
    ];
    v_group    text;
    v_found    int;
    v_ids      text[];
    v_fks      text[];
    v_n        bigint;
BEGIN
    -- 1. The target is the evidence fixture, nothing else.
    SELECT count(*), max(basin_group) INTO v_found, v_group FROM core.basin WHERE basin_id = c_basin;
    IF v_found <> 1 OR v_group IS DISTINCT FROM 'evidence-only' THEN
        RAISE EXCEPTION '#1729: % rows for %, basin_group=% (want 1 row, evidence-only)', v_found, c_basin, v_group;
    END IF;

    -- 2. Live FK dependents of the seven parents == the inventoried 20.
    SELECT coalesce(array_agg(fk ORDER BY fk COLLATE "C"), '{}') INTO v_fks
    FROM (
        SELECT c.conrelid::regclass::text || '|' || c.confrelid::regclass::text || '|' || pg_get_constraintdef(c.oid) AS fk
        FROM pg_constraint c
        JOIN pg_class r ON r.oid = c.conrelid
        JOIN pg_namespace n ON n.oid = r.relnamespace
        WHERE c.contype = 'f'
          AND n.nspname <> '_timescaledb_internal'
          AND c.confrelid IN (
              'core.basin'::regclass, 'core.basin_version'::regclass, 'core.river_network_version'::regclass,
              'core.mesh_version'::regclass, 'core.model_instance'::regclass, 'met.met_station'::regclass,
              'met.forcing_version'::regclass
          )
    ) live;
    IF v_fks IS DISTINCT FROM (SELECT array_agg(fk ORDER BY fk COLLATE "C") FROM unnest(c_expected_fks) fk) THEN
        RAISE EXCEPTION '#1729: live FK set differs; unexpected=% missing=%',
            ARRAY(SELECT unnest(v_fks) EXCEPT SELECT unnest(c_expected_fks)),
            ARRAY(SELECT unnest(c_expected_fks) EXCEPT SELECT unnest(v_fks));
    END IF;

    -- 3. No user trigger on any table deleted from.
    SELECT count(*) INTO v_n FROM pg_trigger
    WHERE NOT tgisinternal
      AND tgrelid IN (
          'core.basin'::regclass, 'core.basin_version'::regclass, 'core.river_network_version'::regclass,
          'core.mesh_version'::regclass, 'core.model_instance'::regclass, 'met.met_station'::regclass
      );
    IF v_n <> 0 THEN
        RAISE EXCEPTION '#1729: % user trigger(s) on the delete targets', v_n;
    END IF;

    -- 4. The id sets under the basin are exactly the inventoried ones.
    SELECT coalesce(array_agg(basin_version_id ORDER BY basin_version_id), '{}') INTO v_ids
    FROM core.basin_version WHERE basin_id = c_basin;
    IF v_ids <> ARRAY[c_bv] THEN
        RAISE EXCEPTION '#1729: basin_version set % (want {%})', v_ids, c_bv;
    END IF;
    SELECT coalesce(array_agg(river_network_version_id ORDER BY river_network_version_id), '{}') INTO v_ids
    FROM core.river_network_version WHERE basin_version_id = c_bv;
    IF v_ids <> ARRAY[c_rnv] THEN
        RAISE EXCEPTION '#1729: river_network_version set % (want {%})', v_ids, c_rnv;
    END IF;
    SELECT coalesce(array_agg(mesh_version_id ORDER BY mesh_version_id), '{}') INTO v_ids
    FROM core.mesh_version WHERE basin_version_id = c_bv;
    IF v_ids <> ARRAY[c_mesh] THEN
        RAISE EXCEPTION '#1729: mesh_version set % (want {%})', v_ids, c_mesh;
    END IF;
    SELECT coalesce(array_agg(model_id ORDER BY model_id COLLATE "C"), '{}') INTO v_ids
    FROM core.model_instance
    WHERE basin_version_id = c_bv OR river_network_version_id = c_rnv OR mesh_version_id = c_mesh;
    IF v_ids <> c_models THEN
        RAISE EXCEPTION '#1729: model_instance set % (want %)', v_ids, c_models;
    END IF;
    SELECT coalesce(array_agg(station_id ORDER BY station_id COLLATE "C"), '{}') INTO v_ids
    FROM met.met_station WHERE basin_version_id = c_bv;
    IF v_ids <> c_stations THEN
        RAISE EXCEPTION '#1729: met_station set % (want %)', v_ids, c_stations;
    END IF;

    -- 5. Every non-hypertable FK dependent holds nothing for them (equality
    --    predicates only; see the header for the hypertables).
    SELECT count(*) INTO v_n FROM core.river_segment WHERE river_network_version_id = c_rnv;
    IF v_n <> 0 THEN RAISE EXCEPTION '#1729: core.river_segment has % row(s)', v_n; END IF;
    SELECT count(*) INTO v_n FROM core.river_segment_crosswalk WHERE river_network_version_id = c_rnv;
    IF v_n <> 0 THEN RAISE EXCEPTION '#1729: core.river_segment_crosswalk has % row(s)', v_n; END IF;
    SELECT count(*) INTO v_n FROM met.interp_weight WHERE model_id = ANY (c_models);
    IF v_n <> 0 THEN RAISE EXCEPTION '#1729: met.interp_weight has % row(s) by model_id', v_n; END IF;
    SELECT count(*) INTO v_n FROM met.interp_weight WHERE station_id = ANY (c_stations);
    IF v_n <> 0 THEN RAISE EXCEPTION '#1729: met.interp_weight has % row(s) by station_id', v_n; END IF;
    SELECT count(*) INTO v_n FROM met.forcing_version WHERE model_id = ANY (c_models);
    IF v_n <> 0 THEN RAISE EXCEPTION '#1729: met.forcing_version has % row(s)', v_n; END IF;
    SELECT count(*) INTO v_n FROM hydro.hydro_run WHERE basin_version_id = c_bv;
    IF v_n <> 0 THEN RAISE EXCEPTION '#1729: hydro.hydro_run has % row(s) by basin_version_id', v_n; END IF;
    SELECT count(*) INTO v_n FROM hydro.hydro_run WHERE model_id = ANY (c_models);
    IF v_n <> 0 THEN RAISE EXCEPTION '#1729: hydro.hydro_run has % row(s) by model_id', v_n; END IF;
    SELECT count(*) INTO v_n FROM hydro.state_snapshot WHERE model_id = ANY (c_models);
    IF v_n <> 0 THEN RAISE EXCEPTION '#1729: hydro.state_snapshot has % row(s)', v_n; END IF;
    SELECT count(*) INTO v_n FROM flood.flood_frequency_curve WHERE model_id = ANY (c_models);
    IF v_n <> 0 THEN RAISE EXCEPTION '#1729: flood.flood_frequency_curve has % row(s)', v_n; END IF;

    -- 6. Retained, reported for the receipt: references without an FK
    --    (equality predicates only; node-27 measured 0 for all but audit_log).
    SELECT count(*) INTO v_n FROM ops.audit_log
    WHERE entity_id = ANY (ARRAY[c_basin, c_bv, c_rnv, c_mesh] || c_models || c_stations);
    RAISE NOTICE '#1729 retained ops.audit_log rows (entity_id equality): %', v_n;
    SELECT count(*) INTO v_n FROM ops.pipeline_job WHERE model_id = ANY (c_models);
    RAISE NOTICE '#1729 retained ops.pipeline_job rows (model_id): %', v_n;
    SELECT count(*) INTO v_n FROM hydro.state_snapshot WHERE cloned_from_model_id = ANY (c_models);
    RAISE NOTICE '#1729 retained hydro.state_snapshot rows (cloned_from_model_id): %', v_n;
    SELECT count(*) INTO v_n FROM flood.return_period_result
    WHERE model_id = ANY (c_models) OR basin_version_id = c_bv OR river_network_version_id = c_rnv;
    RAISE NOTICE '#1729 retained flood.return_period_result rows: %', v_n;

    -- 7. Lock every row the backup below writes and the delete after it
    --    removes, parents first; same predicates as the COPYs and DELETEs.
    SELECT count(*) INTO v_n FROM (
        SELECT 1 FROM core.basin WHERE basin_id = c_basin AND basin_group = 'evidence-only' FOR UPDATE
    ) locked;
    IF v_n <> 1 THEN RAISE EXCEPTION '#1729: locked % core.basin row(s) (want 1)', v_n; END IF;
    SELECT count(*) INTO v_n FROM (
        SELECT 1 FROM core.basin_version WHERE basin_version_id = c_bv AND basin_id = c_basin FOR UPDATE
    ) locked;
    IF v_n <> 1 THEN RAISE EXCEPTION '#1729: locked % core.basin_version row(s) (want 1)', v_n; END IF;
    SELECT count(*) INTO v_n FROM (
        SELECT 1 FROM core.river_network_version
        WHERE river_network_version_id = c_rnv AND basin_version_id = c_bv FOR UPDATE
    ) locked;
    IF v_n <> 1 THEN RAISE EXCEPTION '#1729: locked % core.river_network_version row(s) (want 1)', v_n; END IF;
    SELECT count(*) INTO v_n FROM (
        SELECT 1 FROM core.mesh_version WHERE mesh_version_id = c_mesh AND basin_version_id = c_bv FOR UPDATE
    ) locked;
    IF v_n <> 1 THEN RAISE EXCEPTION '#1729: locked % core.mesh_version row(s) (want 1)', v_n; END IF;
    SELECT count(*) INTO v_n FROM (
        SELECT 1 FROM core.model_instance WHERE model_id = ANY (c_models) AND basin_version_id = c_bv FOR UPDATE
    ) locked;
    IF v_n <> 2 THEN RAISE EXCEPTION '#1729: locked % core.model_instance row(s) (want 2)', v_n; END IF;
    SELECT count(*) INTO v_n FROM (
        SELECT 1 FROM met.met_station WHERE basin_version_id = c_bv AND station_id = ANY (c_stations) FOR UPDATE
    ) locked;
    IF v_n <> 6 THEN RAISE EXCEPTION '#1729: locked % met.met_station row(s) (want 6)', v_n; END IF;
    RAISE NOTICE '#1729 locked basin=1 basin_version=1 river_network_version=1 mesh_version=1 model_instance=2 met_station=6';
END
$$;

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
    WHERE basin_id = 'basin__evidence_cmfd_p02_synth' AND basin_group = 'evidence-only'
    ORDER BY basin_id
) TO STDOUT;

-- @copy-out core.basin_version
COPY (
    SELECT
        basin_version_id, basin_id, version_label, geom, active_flag, valid_from, valid_to,
        source_uri, checksum, created_at, basin_version_key
    FROM core.basin_version
    WHERE basin_version_id = 'basin__evidence_cmfd_p02_synth__v1' AND basin_id = 'basin__evidence_cmfd_p02_synth'
    ORDER BY basin_version_id
) TO STDOUT;

-- @copy-out core.river_network_version
COPY (
    SELECT
        river_network_version_id, basin_version_id, version_label, segment_count, source_uri,
        checksum, created_at, river_network_version_key, geometry_generation
    FROM core.river_network_version
    WHERE river_network_version_id = 'rnw__evidence_cmfd_p02_synth__v1'
      AND basin_version_id = 'basin__evidence_cmfd_p02_synth__v1'
    ORDER BY river_network_version_id
) TO STDOUT;

-- @copy-out core.mesh_version
COPY (
    SELECT
        mesh_version_id, basin_version_id, version_label, mesh_uri, checksum, properties_json,
        created_at
    FROM core.mesh_version
    WHERE mesh_version_id = 'mesh__evidence_cmfd_p02_synth__v1'
      AND basin_version_id = 'basin__evidence_cmfd_p02_synth__v1'
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
    WHERE model_id = ANY (ARRAY['dg_10d27a62b35b39cb5a6f9d10f7fff6e9', 'model__evidence_cmfd_p02_synth__v1'])
      AND basin_version_id = 'basin__evidence_cmfd_p02_synth__v1'
    ORDER BY model_id
) TO STDOUT;

-- @copy-out met.met_station
COPY (
    SELECT
        station_id, basin_version_id, station_name, geom, elevation_m, station_role, active_flag,
        properties_json, created_at, superseded_at, grid_snapshot_id, station_key
    FROM met.met_station
    WHERE basin_version_id = 'basin__evidence_cmfd_p02_synth__v1'
      AND station_id = ANY (ARRAY[
          'synth-mip-m1-v2::cell:cell-0100.00-0030.00',
          'synth-mip-m1-v2::cell:cell-0100.00-0030.50',
          'synth-mip-m1-v2::cell:cell-0100.50-0030.00',
          'synth-station-001',
          'synth-station-002',
          'synth-station-003'
      ])
    ORDER BY station_id
) TO STDOUT;

-- 8. Delete, children first; every step must remove exactly the inventory
--    (the rows locked and backed up above).
DO $$
DECLARE
    c_basin    constant text   := 'basin__evidence_cmfd_p02_synth';
    c_bv       constant text   := 'basin__evidence_cmfd_p02_synth__v1';
    c_rnv      constant text   := 'rnw__evidence_cmfd_p02_synth__v1';
    c_mesh     constant text   := 'mesh__evidence_cmfd_p02_synth__v1';
    c_models   constant text[] := ARRAY[
        'dg_10d27a62b35b39cb5a6f9d10f7fff6e9',
        'model__evidence_cmfd_p02_synth__v1'
    ];
    c_stations constant text[] := ARRAY[
        'synth-mip-m1-v2::cell:cell-0100.00-0030.00',
        'synth-mip-m1-v2::cell:cell-0100.00-0030.50',
        'synth-mip-m1-v2::cell:cell-0100.50-0030.00',
        'synth-station-001',
        'synth-station-002',
        'synth-station-003'
    ];
    v_n        bigint;
    v_started  timestamptz;
BEGIN
    v_started := clock_timestamp();
    DELETE FROM met.met_station WHERE basin_version_id = c_bv AND station_id = ANY (c_stations);
    GET DIAGNOSTICS v_n = ROW_COUNT;
    IF v_n <> 6 THEN RAISE EXCEPTION '#1729: deleted % met.met_station row(s) (want 6)', v_n; END IF;
    RAISE NOTICE '#1729 deleted met.met_station=% in %', v_n, clock_timestamp() - v_started;

    v_started := clock_timestamp();
    DELETE FROM core.model_instance WHERE model_id = ANY (c_models) AND basin_version_id = c_bv;
    GET DIAGNOSTICS v_n = ROW_COUNT;
    IF v_n <> 2 THEN RAISE EXCEPTION '#1729: deleted % core.model_instance row(s) (want 2)', v_n; END IF;
    RAISE NOTICE '#1729 deleted core.model_instance=% in %', v_n, clock_timestamp() - v_started;

    v_started := clock_timestamp();
    DELETE FROM core.mesh_version WHERE mesh_version_id = c_mesh AND basin_version_id = c_bv;
    GET DIAGNOSTICS v_n = ROW_COUNT;
    IF v_n <> 1 THEN RAISE EXCEPTION '#1729: deleted % core.mesh_version row(s) (want 1)', v_n; END IF;
    RAISE NOTICE '#1729 deleted core.mesh_version=% in %', v_n, clock_timestamp() - v_started;

    v_started := clock_timestamp();
    DELETE FROM core.river_network_version WHERE river_network_version_id = c_rnv AND basin_version_id = c_bv;
    GET DIAGNOSTICS v_n = ROW_COUNT;
    IF v_n <> 1 THEN RAISE EXCEPTION '#1729: deleted % core.river_network_version row(s) (want 1)', v_n; END IF;
    RAISE NOTICE '#1729 deleted core.river_network_version=% in %', v_n, clock_timestamp() - v_started;

    v_started := clock_timestamp();
    DELETE FROM core.basin_version WHERE basin_version_id = c_bv AND basin_id = c_basin;
    GET DIAGNOSTICS v_n = ROW_COUNT;
    IF v_n <> 1 THEN RAISE EXCEPTION '#1729: deleted % core.basin_version row(s) (want 1)', v_n; END IF;
    RAISE NOTICE '#1729 deleted core.basin_version=% in %', v_n, clock_timestamp() - v_started;

    v_started := clock_timestamp();
    DELETE FROM core.basin WHERE basin_id = c_basin AND basin_group = 'evidence-only';
    GET DIAGNOSTICS v_n = ROW_COUNT;
    IF v_n <> 1 THEN RAISE EXCEPTION '#1729: deleted % core.basin row(s) (want 1)', v_n; END IF;
    RAISE NOTICE '#1729 deleted core.basin=% in %', v_n, clock_timestamp() - v_started;
END
$$;
