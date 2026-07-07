-- CMFD P0.2 task 2.3 evidence-only registration of synthetic direct-grid contract on node-27
-- Non-production identifiers: prefix "basin__evidence_cmfd_p02_synth" / "rnw__..." / "model__..."
-- All rows carry active_flag=false. 13 production model_instance rows MUST remain untouched.

BEGIN;

-- Pre-check: exactly 13 production model_instance rows (matches design invariant)
DO $$
BEGIN
  IF (SELECT count(*) FROM core.model_instance) != 13 THEN
    RAISE EXCEPTION 'PRECHECK FAIL: expected 13 pre-existing model_instance rows, got %', (SELECT count(*) FROM core.model_instance);
  END IF;
  IF (SELECT count(*) FROM core.model_instance WHERE model_id LIKE 'model__evidence%%') > 0 THEN
    RAISE EXCEPTION 'PRECHECK FAIL: evidence model_instance already exists (rerun?)';
  END IF;
END $$;

-- 1. Synthetic basin
INSERT INTO core.basin (basin_id, basin_name, basin_group, description) VALUES (
  'basin__evidence_cmfd_p02_synth',
  'CMFD P0.2 Synthetic Evidence Basin',
  'evidence-only',
  'Non-production evidence container for cmfd-direct-grid-platform-readiness #891 task 2.3 synthetic direct-grid contract. NOT a real basin. See openspec/changes/cmfd-direct-grid-platform-readiness/evidence/synthetic-package/README.md.'
);

-- 2. Synthetic basin_version (active_flag=false, tiny polygon covering the 3 synthetic stations)
INSERT INTO core.basin_version (basin_version_id, basin_id, version_label, geom, active_flag, source_uri, checksum) VALUES (
  'basin__evidence_cmfd_p02_synth__v1',
  'basin__evidence_cmfd_p02_synth',
  'cmfd-p0.2-synth-v1',
  ST_GeomFromText('MULTIPOLYGON(((99.9 29.9, 100.6 29.9, 100.6 30.6, 99.9 30.6, 99.9 29.9)))', 4490),
  false,
  'https://github.com/DankerMu/SHUD-NWM/tree/master/openspec/changes/cmfd-direct-grid-platform-readiness/evidence/synthetic-package',
  '0baeaf810241b4bc06b129acc0785b1840b79fb32a664b9d42817d3a33aadae5'
);

-- 3. Synthetic river_network_version (segment_count=0; evidence-only)
INSERT INTO core.river_network_version (river_network_version_id, basin_version_id, version_label, segment_count, source_uri, checksum) VALUES (
  'rnw__evidence_cmfd_p02_synth__v1',
  'basin__evidence_cmfd_p02_synth__v1',
  'cmfd-p0.2-synth-v1',
  0,
  NULL,
  NULL
);

-- 4. Synthetic model_instance carrying resource_profile.direct_grid_forcing = the §7.2 binding manifest
INSERT INTO core.model_instance (
  model_id, basin_version_id, river_network_version_id,
  mesh_version_id, calibration_version_id,
  shud_code_version, rshud_code_version, autoshud_code_version,
  container_image, model_package_uri,
  active_flag, resource_profile, lifecycle_state
) VALUES (
  'model__evidence_cmfd_p02_synth__v1',
  'basin__evidence_cmfd_p02_synth__v1',
  'rnw__evidence_cmfd_p02_synth__v1',
  -- mesh_version_id, calibration_version_id are NOT NULL TEXT with no FK (verified via information_schema);
  -- evidence-only string identifiers are correct here — this row is not a real model run.
  'mesh__evidence_cmfd_p02_synth__v1',
  'calib__evidence_cmfd_p02_synth__v1',
  -- shud_code_version is NOT NULL; use the production string ('basins-shud') to match §7.1 shape.
  -- The binary path/version is NOT rebuilt or re-exercised here (evidence-only registration, not a run).
  'basins-shud',
  NULL, NULL, NULL,
  'https://github.com/DankerMu/SHUD-NWM/tree/master/openspec/changes/cmfd-direct-grid-platform-readiness/evidence/synthetic-package',
  false,
  jsonb_build_object(
    'direct_grid_forcing', jsonb_build_object(
      'forcing_mapping_mode', 'direct_grid',
      'binding_uri', 'synth://cmfd-p0.2-direct-grid-evidence/v1',
      'binding_checksum', 'cdf0859b88828d5d4f16c22954b78bf0c36a9b838016b8a91174bdbf39a5dc07',
      'model_input_package_id', 'synth-basin-v1',
      'sp_att_path', 'input_dir/synth-basin/synth-basin.sp.att',
      'sp_att_checksum', '74a64acaab43c7bc61ea9e0eccc83f1116e04b3f73905a39e8f7a4e47b517dde',
      'applicable_source_ids', jsonb_build_array('cmfd'),
      'grid_id', 'synth-grid-p0.2-v1',
      'grid_signature', 'afafe1c814ad6b7a212455c2c8f25d6abaa22f3dd991f232d3749ff7f7d48449',
      'station_bindings', jsonb_build_array(
        jsonb_build_object(
          'station_id', 'synth-station-001',
          'shud_forcing_index', 1,
          'forcing_filename', 'station-001.csv',
          'longitude', 100.0, 'latitude', 30.0,
          'x', 1, 'y', 1, 'z', 100,
          'grid_id', 'synth-grid-p0.2-v1',
          'grid_cell_id', 'cell-0100.00-0030.00'
        ),
        jsonb_build_object(
          'station_id', 'synth-station-002',
          'shud_forcing_index', 2,
          'forcing_filename', 'station-002.csv',
          'longitude', 100.5, 'latitude', 30.0,
          'x', 2, 'y', 1, 'z', 150,
          'grid_id', 'synth-grid-p0.2-v1',
          'grid_cell_id', 'cell-0100.50-0030.00'
        ),
        jsonb_build_object(
          'station_id', 'synth-station-003',
          'shud_forcing_index', 3,
          'forcing_filename', 'station-003.csv',
          'longitude', 100.0, 'latitude', 30.5,
          'x', 1, 'y', 2, 'z', 200,
          'grid_id', 'synth-grid-p0.2-v1',
          'grid_cell_id', 'cell-0100.00-0030.50'
        )
      )
    )
  ),
  -- lifecycle_state CHECK: ANY({inactive,active,deprecated,superseded}) AND (active_flag=false => lifecycle_state<>'active').
  -- 'inactive' is the correct evidence-only slot (row exists but is not eligible for scheduling).
  'inactive'
);

-- 5. Synthetic met.met_station mirror (3 rows, active_flag=false, station_role='forcing_proxy')
INSERT INTO met.met_station (station_id, basin_version_id, station_name, geom, elevation_m, station_role, active_flag, properties_json) VALUES
  ('synth-station-001', 'basin__evidence_cmfd_p02_synth__v1', 'CMFD P0.2 Synthetic Station 001',
   ST_SetSRID(ST_MakePoint(100.0, 30.0), 4490), 100, 'forcing_proxy', false, '{"source": "synth", "evidence_only": true}'::jsonb),
  ('synth-station-002', 'basin__evidence_cmfd_p02_synth__v1', 'CMFD P0.2 Synthetic Station 002',
   ST_SetSRID(ST_MakePoint(100.5, 30.0), 4490), 150, 'forcing_proxy', false, '{"source": "synth", "evidence_only": true}'::jsonb),
  ('synth-station-003', 'basin__evidence_cmfd_p02_synth__v1', 'CMFD P0.2 Synthetic Station 003',
   ST_SetSRID(ST_MakePoint(100.0, 30.5), 4490), 200, 'forcing_proxy', false, '{"source": "synth", "evidence_only": true}'::jsonb);

-- Post-check: 13 production model_instance rows still active_flag=true; 1 new evidence row with active_flag=false
DO $$
BEGIN
  IF (SELECT count(*) FROM core.model_instance WHERE active_flag=true) != 13 THEN
    RAISE EXCEPTION 'POSTCHECK FAIL: production active model_instance count changed from 13 to %', (SELECT count(*) FROM core.model_instance WHERE active_flag=true);
  END IF;
  IF (SELECT count(*) FROM core.model_instance WHERE model_id='model__evidence_cmfd_p02_synth__v1' AND active_flag=false) != 1 THEN
    RAISE EXCEPTION 'POSTCHECK FAIL: evidence model_instance not inserted with active_flag=false';
  END IF;
  IF (SELECT count(*) FROM met.met_station WHERE basin_version_id='basin__evidence_cmfd_p02_synth__v1' AND active_flag=true) != 0 THEN
    RAISE EXCEPTION 'POSTCHECK FAIL: evidence met_station mirror should all be active_flag=false';
  END IF;
END $$;

COMMIT;

-- Report row identifiers and identity
SELECT model_id, basin_version_id, river_network_version_id, active_flag, lifecycle_state, resource_profile->'direct_grid_forcing'->>'binding_uri' AS binding_uri, resource_profile->'direct_grid_forcing'->>'forcing_mapping_mode' AS mode
FROM core.model_instance WHERE model_id = 'model__evidence_cmfd_p02_synth__v1';
