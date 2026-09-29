-- @settings nhms.manifest_basins nhms.probe_timeout_s
--
-- #2621: delete the seven same-day rename leftovers basins_dnzh_{mdzh,mj,mnzh,qtj}
-- and basins_xinan_{dulongjiang,lancangjiang,nujiang} (re-imported on 2026-09-22
-- as basins_se_* / basins_sw_*) and every registry row under them from the
-- node-27 primary, in FK order, in ONE transaction that first locks and backs
-- up exactly the rows it then deletes. Retired basins are NOT rename leftovers
-- and never go through this file (operating-scope.md §7).
--
-- Run ONLY through scripts/ops/node27_oneshot_sql.py, with a FRESH --copy-dir
-- per run (the runner refuses this file without one and never overwrites a
-- copy file) and --set nhms.manifest_basins=<the basin_id set of node-22
-- manifest-last.json, comma separated>. The runner refuses every other --set
-- key (@settings above). Operator steps (openspec change
-- basin-catalog-semantics-and-rename-leftovers, tasks 3.2-3.3):
--   0. pause the writers. node-27 has NO TimescaleDB compression/retention
--      policy jobs (timescaledb_information.jobs holds only the Telemetry
--      Reporter and the Error Log Retention Policy, 2026-09-29); compression,
--      retention and registry/ingest writes are user systemd timers:
--        systemctl --user list-timers --all --no-pager     # record: before
--        systemctl --user stop nhms-node27-timeseries-compression.timer \
--          nhms-node27-timeseries-retention.timer nhms-node27-autopipe.timer
--        systemctl --user is-active nhms-node27-timeseries-compression.service \
--          nhms-node27-timeseries-retention.service nhms-node27-autopipe.service
--      Stopping a timer does not stop a running service instance: every one
--      of the three must print `inactive` before going on (wait, never kill).
--      The file-level timers (download, raw-retention, mvt-cache-retention)
--      keep running;
--   1. dry-run (the runner default: rolled back; NOTICEs carry every gate,
--      count, per-step and per-probe timing) with
--      --copy-dir /home/nwm/tmp/2621-delete-dry-<ts>/ -- that directory is the
--      dry-run's alone, NEVER reuse it for step 3; then
--        systemctl --user start <the three timers>
--        systemctl --user list-timers --all --no-pager     # record: after
--   2. STOP: the owner reviews the dry-run receipt. A probe that hit its
--      statement_timeout is reported to the owner, not retried with a larger
--      one;
--   3. only after the owner confirmed: step 0 again, --apply with a new
--      --copy-dir /home/nwm/tmp/2621-delete-apply-<ts>/ and
--      --set nhms.probe_timeout_s=<2 x the largest dry-run probe, 60..3600>;
--      the eight NAME.copy files it writes are the backup of exactly the rows
--      it deleted, fsynced before COMMIT. Start the three timers, record
--      list-timers after, and copy that directory off node-27;
--   4. restore, if ever needed: node27_2621_delete_rename_leftovers_rollback.sql
--      with --copy-dir pointing at the step-3 directory.
-- Disk: each run (the dry-run too) writes the eight copy files, on the order
-- of hundreds of MB (85196 of ~555k river_segment rows, whose geometry
-- dominates); `df -h / /home` before each run.
--
-- Fail-closed: any deviation below RAISEs, which rolls back every delete. The
-- delete scope is pinned in pg_temp.n2621_target / n2621_model (node-27
-- read-only inventory, 2026-09-29); no setting can widen or recount it.
--   * settings: nhms.manifest_basins non-empty; nhms.probe_timeout_s unset
--     (3600) or an integer in 60..3600; this role may set
--     session_replication_role = replica (tried up front, so a refusal costs
--     no probe time);
--   * column-set guard, byte-identical to the rollback's;
--   * each target exists, is not evidence-only and is NOT in the manifest; its
--     successor exists, IS in the manifest and has an active model;
--   * the live FK set whose parent is a table this delete touches or cascades
--     through equals the 21 non-chunk FKs inventoried on node-27
--     (TimescaleDB per-chunk copies in _timescaledb_internal excluded); no user
--     trigger on the eight tables deleted from;
--   * per target: exactly its one basin_version / river_network_version /
--     mesh_version and the inventoried river_segment / crosswalk / met_station
--     counts; the target stations reference exactly the two pinned
--     canonical_grid_snapshot ids, 1145 each (met_station.grid_snapshot_id is
--     the one FK out of the eight tables; the rollback asserts both parents);
--     the model union (basin_version OR river network OR mesh under a
--     target) is exactly the 21 pinned models, none active; zero hydro_run
--     (by basin_version and by model), forcing_version, interp_weight (by
--     model and by station), state_snapshot (model_id and
--     cloned_from_model_id), run_display_coverage and ops.pipeline_job rows --
--     each of these RAISEs, none is report-only;
--   * every row to delete is locked, parents first (basin -> basin_version ->
--     river_network_version -> mesh_version -> model_instance -> met_station ->
--     river_segment -> river_segment_crosswalk), each lock asserting the count.
--     A locked parent blocks any new FK child row, and the locked crosswalk
--     rows cannot change under the backup;
--   * Gate A2, with the rows locked: hydro.river_timeseries.river_segment_key,
--     met.forcing_station_timeseries.station_key and
--     met.forcing_station_timeseries_legacy.station_id hold 0 rows for the
--     target keys. `= ANY (<array>)` only (a join or IN-subquery decompresses
--     every chunk); each probe is its own statement under its own
--     statement_timeout (nhms.probe_timeout_s; the timer is armed per
--     top-level statement), timed in a NOTICE. The transaction is held open
--     for the probes' duration -- record it: it holds back the vacuum horizon;
--   * the river_segment and met_station DELETEs run with
--     session_replication_role = replica (their RI lookups would scan every
--     hypertable chunk and the station-keyed interp_weight per row: #1732 took
--     4 min 44 s for 8394 rows), set right before and back to origin right
--     after each, asserted. Gate A2 before and Gate B after stand in for the
--     skipped RI checks. Every other DELETE keeps its RI checks;
--   * Gate B: no plain-table row references a deleted parent.
-- Retained, reported only (the only two): ops.audit_log (entity_id equality)
-- and met.best_available_selection (no FK; text forcing_version_id only, and
-- the targets own no forcing_version).

SET LOCAL lock_timeout = '10s';
SET LOCAL statement_timeout = '300s';
SET LOCAL search_path = pg_catalog;
-- The backup's text format (same settings as the rollback script).
SET LOCAL DateStyle = 'ISO, YMD';
SET LOCAL TimeZone = 'UTC';
SET LOCAL extra_float_digits = 3;

-- The pinned delete scope (node-27 read-only inventory, 2026-09-29).
CREATE TEMP TABLE n2621_target (
    basin_id           text PRIMARY KEY,
    successor_basin_id text NOT NULL UNIQUE,
    bv                 text NOT NULL UNIQUE,
    rnv                text NOT NULL UNIQUE,
    mesh               text NOT NULL UNIQUE,
    segments           integer NOT NULL,
    crosswalk          integer NOT NULL,
    stations           integer NOT NULL
) ON COMMIT DROP;
INSERT INTO pg_temp.n2621_target VALUES
    ('basins_dnzh_mdzh', 'basins_se_mdzh', 'basins_dnzh_mdzh_vbasins',
     'basins_dnzh_mdzh_rivnet_vbasins', 'basins_dnzh_mdzh_mesh_vbasins', 1332, 2429, 68),
    ('basins_dnzh_mj', 'basins_se_mj', 'basins_dnzh_mj_vbasins',
     'basins_dnzh_mj_rivnet_vbasins', 'basins_dnzh_mj_mesh_vbasins', 5676, 7552, 230),
    ('basins_dnzh_mnzh', 'basins_se_mnzh', 'basins_dnzh_mnzh_vbasins',
     'basins_dnzh_mnzh_rivnet_vbasins', 'basins_dnzh_mnzh_mesh_vbasins', 2810, 3933, 140),
    ('basins_dnzh_qtj', 'basins_se_qtj', 'basins_dnzh_qtj_vbasins',
     'basins_dnzh_qtj_rivnet_vbasins', 'basins_dnzh_qtj_mesh_vbasins', 4860, 8791, 202),
    ('basins_xinan_dulongjiang', 'basins_sw_dulongjiang', 'basins_xinan_dulongjiang_vbasins',
     'basins_xinan_dulongjiang_rivnet_vbasins', 'basins_xinan_dulongjiang_mesh_vbasins', 16772, 14371, 318),
    ('basins_xinan_lancangjiang', 'basins_sw_lancangjiang', 'basins_xinan_lancangjiang_vbasins',
     'basins_xinan_lancangjiang_rivnet_vbasins', 'basins_xinan_lancangjiang_mesh_vbasins', 26376, 24725, 700),
    ('basins_xinan_nujiang', 'basins_sw_nujiang', 'basins_xinan_nujiang_vbasins',
     'basins_xinan_nujiang_rivnet_vbasins', 'basins_xinan_nujiang_mesh_vbasins', 27370, 23670, 632);
CREATE TEMP TABLE n2621_model (
    model_id text PRIMARY KEY,
    basin_id text NOT NULL
) ON COMMIT DROP;
INSERT INTO pg_temp.n2621_model VALUES
    ('basins_dnzh_mdzh_shud', 'basins_dnzh_mdzh'),
    ('dg_09fce53ac1464cc52f57e1900d09d89b', 'basins_dnzh_mdzh'),
    ('dg_f4cbe11623cbb6e134d473be9ae6e7aa', 'basins_dnzh_mdzh'),
    ('basins_dnzh_mj_shud', 'basins_dnzh_mj'),
    ('dg_3790e742fa58b26fda46daa0d5d7161a', 'basins_dnzh_mj'),
    ('dg_a2b9466955265a94a267973e6b6784ab', 'basins_dnzh_mj'),
    ('basins_dnzh_mnzh_shud', 'basins_dnzh_mnzh'),
    ('dg_090c71585b08bee7c92f9fe98fba1c54', 'basins_dnzh_mnzh'),
    ('dg_761c02aad9cb7bbec0ef4eba742dfb73', 'basins_dnzh_mnzh'),
    ('basins_dnzh_qtj_shud', 'basins_dnzh_qtj'),
    ('dg_58ad77fa681132a4bbab8315ae54b238', 'basins_dnzh_qtj'),
    ('dg_946f928581f452bc30d10ce8e1eafc29', 'basins_dnzh_qtj'),
    ('basins_xinan_dulongjiang_shud', 'basins_xinan_dulongjiang'),
    ('dg_b5047053411ae5aafd0dd5dad58d5535', 'basins_xinan_dulongjiang'),
    ('dg_b5dc14495aa8878fa1095107b34ca114', 'basins_xinan_dulongjiang'),
    ('basins_xinan_lancangjiang_shud', 'basins_xinan_lancangjiang'),
    ('dg_cc6b9d6ba0de0cfafb2b29de0bcf398a', 'basins_xinan_lancangjiang'),
    ('dg_f2f293b4936f68cef46482cbc8c7ed2f', 'basins_xinan_lancangjiang'),
    ('basins_xinan_nujiang_shud', 'basins_xinan_nujiang'),
    ('dg_ba0a40ec23bd1171345eb8f3e597a9e3', 'basins_xinan_nujiang'),
    ('dg_f36cbd64aee01fe535920fd6e2facdcb', 'basins_xinan_nujiang');

-- 0. Settings, the pinned totals, and the replica-mode permission.
DO $$
DECLARE
    v_manifest text := current_setting('nhms.manifest_basins', true);
    v_timeout  text := current_setting('nhms.probe_timeout_s', true);
    v_totals   bigint[];
BEGIN
    IF coalesce(btrim(v_manifest), '') = '' THEN
        RAISE EXCEPTION '#2621: nhms.manifest_basins is unset or empty (pass the manifest-last.json basin_id set)';
    END IF;
    IF coalesce(v_timeout, '') <> ''
       AND (v_timeout !~ '^[0-9]{1,4}$' OR v_timeout::integer NOT BETWEEN 60 AND 3600) THEN
        RAISE EXCEPTION '#2621: nhms.probe_timeout_s=% is not an integer number of seconds in 60..3600', v_timeout;
    END IF;
    SELECT ARRAY[count(*), sum(segments), sum(crosswalk), sum(stations), (SELECT count(*) FROM pg_temp.n2621_model)]
    INTO v_totals FROM pg_temp.n2621_target;
    IF v_totals IS DISTINCT FROM ARRAY[7, 85196, 85471, 2290, 21]::bigint[] THEN
        RAISE EXCEPTION '#2621: pinned totals % (want {7,85196,85471,2290,21})', v_totals;
    END IF;
    IF current_setting('session_replication_role') <> 'origin' THEN
        RAISE EXCEPTION '#2621: session_replication_role is % at start (want origin)',
            current_setting('session_replication_role');
    END IF;
    BEGIN
        PERFORM set_config('session_replication_role', 'replica', true);
    EXCEPTION WHEN insufficient_privilege THEN
        RAISE EXCEPTION '#2621: session_replication_role=replica is not permitted for role %; refusing '
            '(no fallback to per-row RI checks over the hypertables)', current_user;
    END;
    PERFORM set_config('session_replication_role', 'origin', true);
    RAISE NOTICE '#2621 settings: manifest basins=%, probe timeout=%s',
        cardinality(string_to_array(v_manifest, ',')), coalesce(nullif(v_timeout, ''), '3600');
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

-- 1-3. Targets, successors, FK inventory, id sets, counts, zero dependents.
DO $$
DECLARE
    -- child | parent | pg_get_constraintdef, sorted (node-27, 2026-09-29).
    c_expected_fks constant text[] := ARRAY[
        'core.basin_version|core.basin|FOREIGN KEY (basin_id) REFERENCES core.basin(basin_id)',
        'core.mesh_version|core.basin_version|FOREIGN KEY (basin_version_id) REFERENCES core.basin_version(basin_version_id)',
        'core.model_instance|core.basin_version|FOREIGN KEY (basin_version_id) REFERENCES core.basin_version(basin_version_id)',
        'core.model_instance|core.river_network_version|FOREIGN KEY (river_network_version_id) REFERENCES core.river_network_version(river_network_version_id)',
        'core.river_network_version|core.basin_version|FOREIGN KEY (basin_version_id) REFERENCES core.basin_version(basin_version_id)',
        'core.river_segment_crosswalk|core.river_segment|FOREIGN KEY (river_segment_id, river_network_version_id) REFERENCES core.river_segment(river_segment_id, river_network_version_id)',
        'core.river_segment|core.river_network_version|FOREIGN KEY (river_network_version_id) REFERENCES core.river_network_version(river_network_version_id)',
        'hydro.hydro_run|core.basin_version|FOREIGN KEY (basin_version_id) REFERENCES core.basin_version(basin_version_id)',
        'hydro.hydro_run|core.model_instance|FOREIGN KEY (model_id) REFERENCES core.model_instance(model_id)',
        'hydro.hydro_run|met.forcing_version|FOREIGN KEY (forcing_version_id) REFERENCES met.forcing_version(forcing_version_id)',
        'hydro.river_timeseries|core.river_segment|FOREIGN KEY (river_segment_key) REFERENCES core.river_segment(river_segment_key)',
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
    v_manifest text[] := ARRAY(
        SELECT btrim(item) FROM unnest(string_to_array(current_setting('nhms.manifest_basins'), ',')) item
        WHERE btrim(item) <> ''
    );
    r          record;
    v_found    bigint;
    v_group    text;
    v_active   bigint;
    v_ids      text[];
    v_want     text[];
    v_fks      text[];
    v_n        bigint;
BEGIN
    -- 1. Targets are live-looking rename leftovers; successors are scheduled.
    FOR r IN SELECT * FROM pg_temp.n2621_target ORDER BY basin_id LOOP
        SELECT count(*), max(basin_group) INTO v_found, v_group FROM core.basin WHERE basin_id = r.basin_id;
        IF v_found <> 1 OR v_group IS NOT DISTINCT FROM 'evidence-only' THEN
            RAISE EXCEPTION '#2621: % row(s) for %, basin_group=% (want 1 row, not evidence-only)',
                v_found, r.basin_id, v_group;
        END IF;
        IF r.basin_id = ANY (v_manifest) THEN
            RAISE EXCEPTION '#2621: % is in nhms.manifest_basins; a scheduled basin is not a rename leftover', r.basin_id;
        END IF;
        IF NOT r.successor_basin_id = ANY (v_manifest) THEN
            RAISE EXCEPTION '#2621: successor % of % is not in nhms.manifest_basins', r.successor_basin_id, r.basin_id;
        END IF;
        SELECT count(*) INTO v_found FROM core.basin WHERE basin_id = r.successor_basin_id;
        SELECT count(*) INTO v_active
        FROM core.model_instance mi JOIN core.basin_version bv ON bv.basin_version_id = mi.basin_version_id
        WHERE bv.basin_id = r.successor_basin_id AND mi.active_flag;
        IF v_found <> 1 OR v_active < 1 THEN
            RAISE EXCEPTION '#2621: successor % of %: % basin row(s), % active model(s) (want 1, at least 1)',
                r.successor_basin_id, r.basin_id, v_found, v_active;
        END IF;
        RAISE NOTICE '#2621 target % -> successor % (active models: %)', r.basin_id, r.successor_basin_id, v_active;
    END LOOP;

    -- 2. Live FK dependents of the touched parents == the inventoried 21; no user trigger.
    SELECT coalesce(array_agg(fk ORDER BY fk COLLATE "C"), '{}') INTO v_fks
    FROM (
        SELECT c.conrelid::regclass::text || '|' || c.confrelid::regclass::text || '|' || pg_get_constraintdef(c.oid) AS fk
        FROM pg_constraint c
        JOIN pg_class rel ON rel.oid = c.conrelid
        JOIN pg_namespace n ON n.oid = rel.relnamespace
        WHERE c.contype = 'f'
          AND n.nspname <> '_timescaledb_internal'
          AND c.confrelid IN (
              'core.basin'::regclass, 'core.basin_version'::regclass, 'core.river_network_version'::regclass,
              'core.mesh_version'::regclass, 'core.model_instance'::regclass, 'met.met_station'::regclass,
              'core.river_segment'::regclass, 'core.river_segment_crosswalk'::regclass, 'met.forcing_version'::regclass
          )
    ) live;
    IF v_fks IS DISTINCT FROM (SELECT array_agg(fk ORDER BY fk COLLATE "C") FROM unnest(c_expected_fks) fk) THEN
        RAISE EXCEPTION '#2621: live FK set differs; unexpected=% missing=%',
            ARRAY(SELECT unnest(v_fks) EXCEPT SELECT unnest(c_expected_fks)),
            ARRAY(SELECT unnest(c_expected_fks) EXCEPT SELECT unnest(v_fks));
    END IF;
    SELECT count(*) INTO v_n FROM pg_trigger
    WHERE NOT tgisinternal
      AND tgrelid IN (
          'core.basin'::regclass, 'core.basin_version'::regclass, 'core.river_network_version'::regclass,
          'core.mesh_version'::regclass, 'core.model_instance'::regclass, 'met.met_station'::regclass,
          'core.river_segment'::regclass, 'core.river_segment_crosswalk'::regclass
      );
    IF v_n <> 0 THEN
        RAISE EXCEPTION '#2621: % user trigger(s) on the delete targets', v_n;
    END IF;

    -- 3. Per target: exactly its versions and the inventoried row counts.
    FOR r IN SELECT * FROM pg_temp.n2621_target ORDER BY basin_id LOOP
        SELECT coalesce(array_agg(basin_version_id ORDER BY basin_version_id COLLATE "C"), '{}') INTO v_ids
        FROM core.basin_version WHERE basin_id = r.basin_id;
        IF v_ids <> ARRAY[r.bv] THEN
            RAISE EXCEPTION '#2621: %: basin_version set % (want {%})', r.basin_id, v_ids, r.bv;
        END IF;
        SELECT coalesce(array_agg(river_network_version_id ORDER BY river_network_version_id COLLATE "C"), '{}')
        INTO v_ids FROM core.river_network_version WHERE basin_version_id = r.bv;
        IF v_ids <> ARRAY[r.rnv] THEN
            RAISE EXCEPTION '#2621: %: river_network_version set % (want {%})', r.basin_id, v_ids, r.rnv;
        END IF;
        SELECT coalesce(array_agg(mesh_version_id ORDER BY mesh_version_id COLLATE "C"), '{}') INTO v_ids
        FROM core.mesh_version WHERE basin_version_id = r.bv;
        IF v_ids <> ARRAY[r.mesh] THEN
            RAISE EXCEPTION '#2621: %: mesh_version set % (want {%})', r.basin_id, v_ids, r.mesh;
        END IF;
        SELECT count(*) INTO v_n FROM core.river_segment WHERE river_network_version_id = r.rnv;
        IF v_n <> r.segments THEN
            RAISE EXCEPTION '#2621: %: river_segment=% (want %)', r.basin_id, v_n, r.segments;
        END IF;
        SELECT count(*) INTO v_n FROM core.river_segment_crosswalk WHERE river_network_version_id = r.rnv;
        IF v_n <> r.crosswalk THEN
            RAISE EXCEPTION '#2621: %: river_segment_crosswalk=% (want %)', r.basin_id, v_n, r.crosswalk;
        END IF;
        SELECT count(*) INTO v_n FROM met.met_station WHERE basin_version_id = r.bv;
        IF v_n <> r.stations THEN
            RAISE EXCEPTION '#2621: %: met_station=% (want %)', r.basin_id, v_n, r.stations;
        END IF;
    END LOOP;

    -- met_station.grid_snapshot_id is the one FK out of the eight tables: the backup
    -- may reference only the two pinned snapshots, whose existence the rollback asserts.
    SELECT coalesce(array_agg(g ORDER BY g COLLATE "C"), '{}') INTO v_ids
    FROM (
        SELECT coalesce(grid_snapshot_id::text, 'NULL') || '=' || count(*) AS g
        FROM met.met_station WHERE basin_version_id IN (SELECT bv FROM pg_temp.n2621_target)
        GROUP BY grid_snapshot_id
    ) snapshots;
    IF v_ids IS DISTINCT FROM ARRAY['2f00657d-07a2-4ae3-b08e-8e670721033e=1145',
                                    'bcd58449-a0b5-4c8a-b2f8-36a2f996a268=1145'] THEN
        RAISE EXCEPTION '#2621: target met_station grid_snapshot_id counts % (want '
            '{2f00657d-07a2-4ae3-b08e-8e670721033e=1145,bcd58449-a0b5-4c8a-b2f8-36a2f996a268=1145})', v_ids;
    END IF;

    -- The model union reaches exactly the 21 pinned models, each under its own target, none active.
    SELECT coalesce(array_agg(model_id ORDER BY model_id COLLATE "C"), '{}') INTO v_ids
    FROM core.model_instance
    WHERE basin_version_id IN (SELECT bv FROM pg_temp.n2621_target)
       OR river_network_version_id IN (SELECT rnv FROM pg_temp.n2621_target)
       OR mesh_version_id IN (SELECT mesh FROM pg_temp.n2621_target);
    SELECT array_agg(model_id ORDER BY model_id COLLATE "C") INTO v_want FROM pg_temp.n2621_model;
    IF v_ids IS DISTINCT FROM v_want THEN
        RAISE EXCEPTION '#2621: model_instance set % (want %)', v_ids, v_want;
    END IF;
    SELECT count(*) INTO v_n
    FROM core.model_instance mi
    JOIN pg_temp.n2621_model m ON m.model_id = mi.model_id
    JOIN pg_temp.n2621_target t ON t.basin_id = m.basin_id
    WHERE mi.basin_version_id = t.bv AND mi.river_network_version_id = t.rnv AND mi.mesh_version_id = t.mesh;
    IF v_n <> 21 THEN
        RAISE EXCEPTION '#2621: % of the 21 models sit under their own target version triple', v_n;
    END IF;
    SELECT count(*) INTO v_n FROM core.model_instance
    WHERE model_id IN (SELECT model_id FROM pg_temp.n2621_model)
      AND (active_flag OR lifecycle_state IS DISTINCT FROM 'inactive');
    IF v_n <> 0 THEN
        RAISE EXCEPTION '#2621: % active model(s) among the targets (active_flag or lifecycle_state <> inactive)', v_n;
    END IF;

    -- Zero business dependents (plain tables; the hypertables are Gate A2's).
    SELECT count(*) INTO v_n FROM hydro.hydro_run WHERE basin_version_id IN (SELECT bv FROM pg_temp.n2621_target);
    IF v_n <> 0 THEN RAISE EXCEPTION '#2621: hydro.hydro_run has % row(s) by basin_version_id', v_n; END IF;
    SELECT count(*) INTO v_n FROM hydro.hydro_run WHERE model_id IN (SELECT model_id FROM pg_temp.n2621_model);
    IF v_n <> 0 THEN RAISE EXCEPTION '#2621: hydro.hydro_run has % row(s) by model_id', v_n; END IF;
    SELECT count(*) INTO v_n FROM met.forcing_version WHERE model_id IN (SELECT model_id FROM pg_temp.n2621_model);
    IF v_n <> 0 THEN RAISE EXCEPTION '#2621: met.forcing_version has % row(s)', v_n; END IF;
    SELECT count(*) INTO v_n FROM met.interp_weight WHERE model_id IN (SELECT model_id FROM pg_temp.n2621_model);
    IF v_n <> 0 THEN RAISE EXCEPTION '#2621: met.interp_weight has % row(s) by model_id', v_n; END IF;
    SELECT count(*) INTO v_n FROM met.interp_weight
    WHERE station_id IN (
        SELECT station_id FROM met.met_station WHERE basin_version_id IN (SELECT bv FROM pg_temp.n2621_target)
    );
    IF v_n <> 0 THEN RAISE EXCEPTION '#2621: met.interp_weight has % row(s) by station_id', v_n; END IF;
    SELECT count(*) INTO v_n FROM hydro.state_snapshot
    WHERE model_id IN (SELECT model_id FROM pg_temp.n2621_model)
       OR cloned_from_model_id IN (SELECT model_id FROM pg_temp.n2621_model);
    IF v_n <> 0 THEN RAISE EXCEPTION '#2621: hydro.state_snapshot has % row(s)', v_n; END IF;
    SELECT count(*) INTO v_n FROM hydro.run_display_coverage c JOIN hydro.hydro_run h ON h.run_id = c.run_id
    WHERE h.model_id IN (SELECT model_id FROM pg_temp.n2621_model)
       OR h.basin_version_id IN (SELECT bv FROM pg_temp.n2621_target);
    IF v_n <> 0 THEN RAISE EXCEPTION '#2621: hydro.run_display_coverage has % row(s)', v_n; END IF;
    SELECT count(*) INTO v_n FROM ops.pipeline_job WHERE model_id IN (SELECT model_id FROM pg_temp.n2621_model);
    IF v_n <> 0 THEN RAISE EXCEPTION '#2621: ops.pipeline_job has % row(s) by model_id', v_n; END IF;

    -- Retained, reported for the receipt (equality predicates only).
    SELECT count(*) INTO v_n FROM ops.audit_log
    WHERE entity_id IN (
        SELECT unnest(ARRAY[basin_id, bv, rnv, mesh]) FROM pg_temp.n2621_target
        UNION ALL SELECT model_id FROM pg_temp.n2621_model
    );
    RAISE NOTICE '#2621 retained ops.audit_log rows (entity_id equality): %', v_n;
    SELECT count(*) INTO v_n FROM met.best_available_selection
    WHERE forcing_version_id IN (
        SELECT forcing_version_id FROM met.forcing_version WHERE model_id IN (SELECT model_id FROM pg_temp.n2621_model)
    );
    RAISE NOTICE '#2621 retained met.best_available_selection rows (forcing_version_id): %', v_n;
END
$$;

-- 4. Lock every row the backup below writes and the delete after it removes,
--    parents first; the same predicates as the COPYs and the DELETEs.
DO $$
DECLARE
    v_n bigint;
    v_counts text := '';
BEGIN
    SELECT count(*) INTO v_n FROM (
        SELECT 1 FROM core.basin WHERE basin_id IN (SELECT basin_id FROM pg_temp.n2621_target) FOR UPDATE
    ) locked;
    IF v_n <> 7 THEN RAISE EXCEPTION '#2621: locked % core.basin row(s) (want 7)', v_n; END IF;
    v_counts := v_counts || ' basin=' || v_n;
    SELECT count(*) INTO v_n FROM (
        SELECT 1 FROM core.basin_version WHERE basin_id IN (SELECT basin_id FROM pg_temp.n2621_target) FOR UPDATE
    ) locked;
    IF v_n <> 7 THEN RAISE EXCEPTION '#2621: locked % core.basin_version row(s) (want 7)', v_n; END IF;
    v_counts := v_counts || ' basin_version=' || v_n;
    SELECT count(*) INTO v_n FROM (
        SELECT 1 FROM core.river_network_version
        WHERE basin_version_id IN (SELECT bv FROM pg_temp.n2621_target) FOR UPDATE
    ) locked;
    IF v_n <> 7 THEN RAISE EXCEPTION '#2621: locked % core.river_network_version row(s) (want 7)', v_n; END IF;
    v_counts := v_counts || ' river_network_version=' || v_n;
    SELECT count(*) INTO v_n FROM (
        SELECT 1 FROM core.mesh_version WHERE basin_version_id IN (SELECT bv FROM pg_temp.n2621_target) FOR UPDATE
    ) locked;
    IF v_n <> 7 THEN RAISE EXCEPTION '#2621: locked % core.mesh_version row(s) (want 7)', v_n; END IF;
    v_counts := v_counts || ' mesh_version=' || v_n;
    SELECT count(*) INTO v_n FROM (
        SELECT 1 FROM core.model_instance WHERE model_id IN (SELECT model_id FROM pg_temp.n2621_model) FOR UPDATE
    ) locked;
    IF v_n <> 21 THEN RAISE EXCEPTION '#2621: locked % core.model_instance row(s) (want 21)', v_n; END IF;
    v_counts := v_counts || ' model_instance=' || v_n;
    SELECT count(*) INTO v_n FROM (
        SELECT 1 FROM met.met_station WHERE basin_version_id IN (SELECT bv FROM pg_temp.n2621_target) FOR UPDATE
    ) locked;
    IF v_n <> 2290 THEN RAISE EXCEPTION '#2621: locked % met.met_station row(s) (want 2290)', v_n; END IF;
    v_counts := v_counts || ' met_station=' || v_n;
    SELECT count(*) INTO v_n FROM (
        SELECT 1 FROM core.river_segment
        WHERE river_network_version_id IN (SELECT rnv FROM pg_temp.n2621_target) FOR UPDATE
    ) locked;
    IF v_n <> 85196 THEN RAISE EXCEPTION '#2621: locked % core.river_segment row(s) (want 85196)', v_n; END IF;
    v_counts := v_counts || ' river_segment=' || v_n;
    SELECT count(*) INTO v_n FROM (
        SELECT 1 FROM core.river_segment_crosswalk
        WHERE river_network_version_id IN (SELECT rnv FROM pg_temp.n2621_target) FOR UPDATE
    ) locked;
    IF v_n <> 85471 THEN RAISE EXCEPTION '#2621: locked % core.river_segment_crosswalk row(s) (want 85471)', v_n; END IF;
    v_counts := v_counts || ' river_segment_crosswalk=' || v_n;
    -- The station ids outlive their rows for Gate B.
    CREATE TEMP TABLE n2621_station ON COMMIT DROP AS
        SELECT station_id, station_key FROM met.met_station
        WHERE basin_version_id IN (SELECT bv FROM pg_temp.n2621_target);
    RAISE NOTICE '#2621 locked%', v_counts;
END
$$;

-- 5. Gate A2: zero hypertable references to the target keys, with the rows
--    locked. Each probe is a statement of its own, under its own budget.
SELECT set_config('statement_timeout', coalesce(nullif(current_setting('nhms.probe_timeout_s', true), ''), '3600') || 's', true);
DO $$
DECLARE
    v_keys    integer[];
    v_n       bigint;
    v_started timestamptz := clock_timestamp();
BEGIN
    SELECT array_agg(river_segment_key ORDER BY river_segment_key) INTO v_keys
    FROM core.river_segment WHERE river_network_version_id IN (SELECT rnv FROM pg_temp.n2621_target);
    SELECT count(*) INTO v_n FROM hydro.river_timeseries WHERE river_segment_key = ANY (v_keys);
    RAISE NOTICE '#2621 probe hydro.river_timeseries.river_segment_key: % row(s) for % key(s) in %',
        v_n, cardinality(v_keys), clock_timestamp() - v_started;
    IF v_n <> 0 THEN
        RAISE EXCEPTION '#2621: hydro.river_timeseries.river_segment_key: % row(s) reference the targets', v_n;
    END IF;
END
$$;
SELECT set_config('statement_timeout', coalesce(nullif(current_setting('nhms.probe_timeout_s', true), ''), '3600') || 's', true);
DO $$
DECLARE
    v_keys    integer[];
    v_n       bigint;
    v_started timestamptz := clock_timestamp();
BEGIN
    SELECT array_agg(station_key ORDER BY station_key) INTO v_keys FROM pg_temp.n2621_station;
    SELECT count(*) INTO v_n FROM met.forcing_station_timeseries WHERE station_key = ANY (v_keys);
    RAISE NOTICE '#2621 probe met.forcing_station_timeseries.station_key: % row(s) for % key(s) in %',
        v_n, cardinality(v_keys), clock_timestamp() - v_started;
    IF v_n <> 0 THEN
        RAISE EXCEPTION '#2621: met.forcing_station_timeseries.station_key: % row(s) reference the targets', v_n;
    END IF;
END
$$;
SELECT set_config('statement_timeout', coalesce(nullif(current_setting('nhms.probe_timeout_s', true), ''), '3600') || 's', true);
DO $$
DECLARE
    v_ids     text[];
    v_n       bigint;
    v_started timestamptz := clock_timestamp();
BEGIN
    SELECT array_agg(station_id ORDER BY station_id COLLATE "C") INTO v_ids FROM pg_temp.n2621_station;
    SELECT count(*) INTO v_n FROM met.forcing_station_timeseries_legacy WHERE station_id = ANY (v_ids);
    RAISE NOTICE '#2621 probe met.forcing_station_timeseries_legacy.station_id: % row(s) for % id(s) in %',
        v_n, cardinality(v_ids), clock_timestamp() - v_started;
    IF v_n <> 0 THEN
        RAISE EXCEPTION '#2621: met.forcing_station_timeseries_legacy.station_id: % row(s) reference the targets', v_n;
    END IF;
    -- Invariant, not a substitute for the probes: every river_timeseries row hangs
    -- off a hydro_run through run_key, and the targets own none (step 3).
    RAISE NOTICE '#2621 invariant: the targets own 0 hydro_run rows, so no river_timeseries run_key can reach them';
END
$$;
SET LOCAL statement_timeout = '300s';

-- @copy-out core.basin
COPY (
    SELECT
        basin_id, basin_name, basin_group, description, created_at
    FROM core.basin
    WHERE basin_id IN (SELECT basin_id FROM pg_temp.n2621_target)
    ORDER BY basin_id
) TO STDOUT;

-- @copy-out core.basin_version
COPY (
    SELECT
        basin_version_id, basin_id, version_label, geom, active_flag, valid_from, valid_to,
        source_uri, checksum, created_at, basin_version_key
    FROM core.basin_version
    WHERE basin_id IN (SELECT basin_id FROM pg_temp.n2621_target)
    ORDER BY basin_version_id
) TO STDOUT;

-- @copy-out core.river_network_version
COPY (
    SELECT
        river_network_version_id, basin_version_id, version_label, segment_count, source_uri,
        checksum, created_at, river_network_version_key, geometry_generation
    FROM core.river_network_version
    WHERE basin_version_id IN (SELECT bv FROM pg_temp.n2621_target)
    ORDER BY river_network_version_id
) TO STDOUT;

-- @copy-out core.mesh_version
COPY (
    SELECT
        mesh_version_id, basin_version_id, version_label, mesh_uri, checksum, properties_json,
        created_at
    FROM core.mesh_version
    WHERE basin_version_id IN (SELECT bv FROM pg_temp.n2621_target)
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
    WHERE model_id IN (SELECT model_id FROM pg_temp.n2621_model)
    ORDER BY model_id
) TO STDOUT;

-- @copy-out met.met_station
COPY (
    SELECT
        station_id, basin_version_id, station_name, geom, elevation_m, station_role, active_flag,
        properties_json, created_at, superseded_at, grid_snapshot_id, station_key
    FROM met.met_station
    WHERE basin_version_id IN (SELECT bv FROM pg_temp.n2621_target)
    ORDER BY station_id
) TO STDOUT;

-- @copy-out core.river_segment
COPY (
    SELECT
        river_segment_id, river_network_version_id, segment_order, downstream_segment_id, length_m,
        geom, properties_json, created_at, river_segment_key
    FROM core.river_segment
    WHERE river_network_version_id IN (SELECT rnv FROM pg_temp.n2621_target)
    ORDER BY river_segment_key
) TO STDOUT;

-- @copy-out core.river_segment_crosswalk
COPY (
    SELECT
        crosswalk_id, river_network_version_id, river_segment_id, source, external_id,
        properties_json, created_at
    FROM core.river_segment_crosswalk
    WHERE river_network_version_id IN (SELECT rnv FROM pg_temp.n2621_target)
    ORDER BY crosswalk_id
) TO STDOUT;

-- 7. Delete, children first; every step must remove exactly the rows locked
--    and backed up above. Replica mode covers the two DELETEs it names only.
DO $$
DECLARE
    v_n       bigint;
    v_started timestamptz;
BEGIN
    v_started := clock_timestamp();
    DELETE FROM core.river_segment_crosswalk WHERE river_network_version_id IN (SELECT rnv FROM pg_temp.n2621_target);
    GET DIAGNOSTICS v_n = ROW_COUNT;
    IF v_n <> 85471 THEN RAISE EXCEPTION '#2621: deleted % core.river_segment_crosswalk row(s) (want 85471)', v_n; END IF;
    RAISE NOTICE '#2621 deleted core.river_segment_crosswalk=% in %', v_n, clock_timestamp() - v_started;

    v_started := clock_timestamp();
    PERFORM set_config('session_replication_role', 'replica', true);
    DELETE FROM core.river_segment WHERE river_network_version_id IN (SELECT rnv FROM pg_temp.n2621_target);
    GET DIAGNOSTICS v_n = ROW_COUNT;
    PERFORM set_config('session_replication_role', 'origin', true);
    IF current_setting('session_replication_role') <> 'origin' THEN
        RAISE EXCEPTION '#2621: session_replication_role=% after the river_segment delete',
            current_setting('session_replication_role');
    END IF;
    IF v_n <> 85196 THEN RAISE EXCEPTION '#2621: deleted % core.river_segment row(s) (want 85196)', v_n; END IF;
    RAISE NOTICE '#2621 deleted core.river_segment=% (session_replication_role=replica) in %',
        v_n, clock_timestamp() - v_started;

    v_started := clock_timestamp();
    PERFORM set_config('session_replication_role', 'replica', true);
    DELETE FROM met.met_station WHERE basin_version_id IN (SELECT bv FROM pg_temp.n2621_target);
    GET DIAGNOSTICS v_n = ROW_COUNT;
    PERFORM set_config('session_replication_role', 'origin', true);
    IF current_setting('session_replication_role') <> 'origin' THEN
        RAISE EXCEPTION '#2621: session_replication_role=% after the met_station delete',
            current_setting('session_replication_role');
    END IF;
    IF v_n <> 2290 THEN RAISE EXCEPTION '#2621: deleted % met.met_station row(s) (want 2290)', v_n; END IF;
    RAISE NOTICE '#2621 deleted met.met_station=% (session_replication_role=replica) in %',
        v_n, clock_timestamp() - v_started;

    v_started := clock_timestamp();
    DELETE FROM core.model_instance WHERE model_id IN (SELECT model_id FROM pg_temp.n2621_model);
    GET DIAGNOSTICS v_n = ROW_COUNT;
    IF v_n <> 21 THEN RAISE EXCEPTION '#2621: deleted % core.model_instance row(s) (want 21)', v_n; END IF;
    RAISE NOTICE '#2621 deleted core.model_instance=% in %', v_n, clock_timestamp() - v_started;

    v_started := clock_timestamp();
    DELETE FROM core.mesh_version WHERE basin_version_id IN (SELECT bv FROM pg_temp.n2621_target);
    GET DIAGNOSTICS v_n = ROW_COUNT;
    IF v_n <> 7 THEN RAISE EXCEPTION '#2621: deleted % core.mesh_version row(s) (want 7)', v_n; END IF;
    RAISE NOTICE '#2621 deleted core.mesh_version=% in %', v_n, clock_timestamp() - v_started;

    v_started := clock_timestamp();
    DELETE FROM core.river_network_version WHERE basin_version_id IN (SELECT bv FROM pg_temp.n2621_target);
    GET DIAGNOSTICS v_n = ROW_COUNT;
    IF v_n <> 7 THEN RAISE EXCEPTION '#2621: deleted % core.river_network_version row(s) (want 7)', v_n; END IF;
    RAISE NOTICE '#2621 deleted core.river_network_version=% in %', v_n, clock_timestamp() - v_started;

    v_started := clock_timestamp();
    DELETE FROM core.basin_version WHERE basin_id IN (SELECT basin_id FROM pg_temp.n2621_target);
    GET DIAGNOSTICS v_n = ROW_COUNT;
    IF v_n <> 7 THEN RAISE EXCEPTION '#2621: deleted % core.basin_version row(s) (want 7)', v_n; END IF;
    RAISE NOTICE '#2621 deleted core.basin_version=% in %', v_n, clock_timestamp() - v_started;

    v_started := clock_timestamp();
    DELETE FROM core.basin WHERE basin_id IN (SELECT basin_id FROM pg_temp.n2621_target);
    GET DIAGNOSTICS v_n = ROW_COUNT;
    IF v_n <> 7 THEN RAISE EXCEPTION '#2621: deleted % core.basin row(s) (want 7)', v_n; END IF;
    RAISE NOTICE '#2621 deleted core.basin=% in %', v_n, clock_timestamp() - v_started;
END
$$;

-- 8. Gate B: no plain-table row references a deleted parent.
DO $$
DECLARE
    v_n bigint;
BEGIN
    IF current_setting('session_replication_role') <> 'origin' THEN
        RAISE EXCEPTION '#2621: session_replication_role=% at gate B', current_setting('session_replication_role');
    END IF;
    SELECT count(*) INTO v_n FROM core.river_segment_crosswalk
    WHERE river_network_version_id IN (SELECT rnv FROM pg_temp.n2621_target);
    IF v_n <> 0 THEN RAISE EXCEPTION '#2621 gate B: core.river_segment_crosswalk has % orphan(s)', v_n; END IF;
    SELECT count(*) INTO v_n FROM core.river_segment
    WHERE river_network_version_id IN (SELECT rnv FROM pg_temp.n2621_target);
    IF v_n <> 0 THEN RAISE EXCEPTION '#2621 gate B: core.river_segment has % orphan(s)', v_n; END IF;
    SELECT count(*) INTO v_n FROM met.met_station WHERE basin_version_id IN (SELECT bv FROM pg_temp.n2621_target);
    IF v_n <> 0 THEN RAISE EXCEPTION '#2621 gate B: met.met_station has % orphan(s)', v_n; END IF;
    SELECT count(*) INTO v_n FROM met.interp_weight
    WHERE model_id IN (SELECT model_id FROM pg_temp.n2621_model)
       OR station_id IN (SELECT station_id FROM pg_temp.n2621_station);
    IF v_n <> 0 THEN RAISE EXCEPTION '#2621 gate B: met.interp_weight has % orphan(s)', v_n; END IF;
    SELECT count(*) INTO v_n FROM hydro.hydro_run
    WHERE model_id IN (SELECT model_id FROM pg_temp.n2621_model)
       OR basin_version_id IN (SELECT bv FROM pg_temp.n2621_target);
    IF v_n <> 0 THEN RAISE EXCEPTION '#2621 gate B: hydro.hydro_run has % orphan(s)', v_n; END IF;
    SELECT count(*) INTO v_n FROM core.model_instance
    WHERE basin_version_id IN (SELECT bv FROM pg_temp.n2621_target)
       OR river_network_version_id IN (SELECT rnv FROM pg_temp.n2621_target)
       OR mesh_version_id IN (SELECT mesh FROM pg_temp.n2621_target);
    IF v_n <> 0 THEN RAISE EXCEPTION '#2621 gate B: core.model_instance has % orphan(s)', v_n; END IF;
    RAISE NOTICE '#2621 gate B: no orphan references';
END
$$;
