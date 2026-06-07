CREATE TABLE IF NOT EXISTS hydro.hydro_run (
  run_id TEXT PRIMARY KEY,
  run_type hydro.run_type NOT NULL,
  scenario_id TEXT NOT NULL,
  model_id TEXT NOT NULL REFERENCES core.model_instance(model_id),
  basin_version_id TEXT NOT NULL REFERENCES core.basin_version(basin_version_id),
  forcing_version_id TEXT REFERENCES met.forcing_version(forcing_version_id),
  init_state_id TEXT,
  source_id TEXT REFERENCES met.data_source(source_id),
  cycle_time TIMESTAMPTZ,
  start_time TIMESTAMPTZ NOT NULL,
  end_time TIMESTAMPTZ NOT NULL,
  status hydro.run_status NOT NULL,
  slurm_job_id TEXT,
  run_manifest_uri TEXT NOT NULL,
  output_uri TEXT,
  log_uri TEXT,
  error_code TEXT,
  error_message TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS hydro.state_snapshot (
  state_id TEXT PRIMARY KEY,
  model_id TEXT NOT NULL REFERENCES core.model_instance(model_id),
  run_id TEXT NOT NULL REFERENCES hydro.hydro_run(run_id),
  valid_time TIMESTAMPTZ NOT NULL,
  state_uri TEXT NOT NULL,
  checksum TEXT NOT NULL,
  usable_flag BOOLEAN NOT NULL DEFAULT false,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  source_id TEXT,
  cycle_id TEXT,
  lead_hours INTEGER,
  model_package_version TEXT,
  model_package_checksum TEXT,
  original_shud_filename TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS state_snapshot_model_source_valid_time_key
  ON hydro.state_snapshot (model_id, COALESCE(source_id, ''), valid_time);

CREATE TABLE IF NOT EXISTS hydro.river_timeseries (
  run_id TEXT NOT NULL,
  basin_version_id TEXT NOT NULL,
  river_network_version_id TEXT NOT NULL,
  river_segment_id TEXT NOT NULL,
  valid_time TIMESTAMPTZ NOT NULL,
  lead_time_hours INT,
  variable TEXT NOT NULL,
  value DOUBLE PRECISION NOT NULL,
  unit TEXT NOT NULL,
  quality_flag TEXT NOT NULL DEFAULT 'ok',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (run_id, river_network_version_id, river_segment_id, variable, valid_time),
  FOREIGN KEY (river_segment_id, river_network_version_id)
    REFERENCES core.river_segment(river_segment_id, river_network_version_id)
);
SELECT create_hypertable('hydro.river_timeseries', 'valid_time', if_not_exists => TRUE);
