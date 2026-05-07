CREATE TABLE IF NOT EXISTS met.data_source (
  source_id TEXT PRIMARY KEY,
  source_name TEXT NOT NULL,
  source_type TEXT NOT NULL,
  status met.source_status NOT NULL,
  native_format TEXT,
  license_status TEXT,
  adapter_name TEXT NOT NULL,
  config_json JSONB NOT NULL DEFAULT '{}',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS met.forecast_cycle (
  cycle_id TEXT PRIMARY KEY,
  source_id TEXT NOT NULL REFERENCES met.data_source(source_id),
  cycle_time TIMESTAMPTZ NOT NULL,
  issue_time TIMESTAMPTZ,
  status met.cycle_status NOT NULL,
  manifest_uri TEXT,
  retry_count INT NOT NULL DEFAULT 0,
  error_code TEXT,
  error_message TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (source_id, cycle_time)
);

CREATE TABLE IF NOT EXISTS met.canonical_met_product (
  canonical_product_id TEXT PRIMARY KEY,
  source_id TEXT NOT NULL REFERENCES met.data_source(source_id),
  source_version TEXT,
  cycle_time TIMESTAMPTZ NOT NULL,
  valid_time TIMESTAMPTZ NOT NULL,
  lead_time_hours INT,
  variable TEXT NOT NULL,
  unit TEXT NOT NULL,
  grid_id TEXT NOT NULL,
  grid_definition_uri TEXT,
  native_time_resolution TEXT,
  native_spatial_resolution TEXT,
  object_uri TEXT NOT NULL,
  checksum TEXT NOT NULL,
  quality_flag TEXT NOT NULL DEFAULT 'ok',
  lineage_json JSONB NOT NULL DEFAULT '{}',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS met.met_station (
  station_id TEXT PRIMARY KEY,
  basin_version_id TEXT NOT NULL REFERENCES core.basin_version(basin_version_id),
  station_name TEXT,
  geom geometry(Point, 4490) NOT NULL,
  elevation_m DOUBLE PRECISION,
  station_role TEXT NOT NULL DEFAULT 'forcing_proxy',
  active_flag BOOLEAN NOT NULL DEFAULT true,
  properties_json JSONB NOT NULL DEFAULT '{}',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS met_station_geom_gix ON met.met_station USING gist (geom);

CREATE TABLE IF NOT EXISTS met.interp_weight (
  weight_id BIGSERIAL PRIMARY KEY,
  source_id TEXT NOT NULL REFERENCES met.data_source(source_id),
  grid_id TEXT NOT NULL,
  model_id TEXT NOT NULL REFERENCES core.model_instance(model_id),
  station_id TEXT NOT NULL REFERENCES met.met_station(station_id),
  variable TEXT NOT NULL,
  grid_cell_id TEXT NOT NULL,
  weight DOUBLE PRECISION NOT NULL,
  method TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (source_id, grid_id, model_id, station_id, variable, grid_cell_id)
);

CREATE TABLE IF NOT EXISTS met.forcing_version (
  forcing_version_id TEXT PRIMARY KEY,
  model_id TEXT NOT NULL REFERENCES core.model_instance(model_id),
  source_id TEXT NOT NULL REFERENCES met.data_source(source_id),
  cycle_time TIMESTAMPTZ,
  start_time TIMESTAMPTZ NOT NULL,
  end_time TIMESTAMPTZ NOT NULL,
  station_count INT NOT NULL,
  forcing_package_uri TEXT NOT NULL,
  checksum TEXT,
  lineage_json JSONB NOT NULL DEFAULT '{}',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS met.forcing_version_component (
  forcing_version_id TEXT NOT NULL REFERENCES met.forcing_version(forcing_version_id),
  canonical_product_id TEXT NOT NULL REFERENCES met.canonical_met_product(canonical_product_id),
  variable TEXT NOT NULL,
  valid_time_start TIMESTAMPTZ,
  valid_time_end TIMESTAMPTZ,
  role TEXT NOT NULL DEFAULT 'forcing_input',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (forcing_version_id, canonical_product_id, variable)
);

CREATE TABLE IF NOT EXISTS met.forcing_station_timeseries (
  forcing_version_id TEXT NOT NULL REFERENCES met.forcing_version(forcing_version_id),
  basin_version_id TEXT NOT NULL,
  station_id TEXT NOT NULL REFERENCES met.met_station(station_id),
  valid_time TIMESTAMPTZ NOT NULL,
  source_id TEXT NOT NULL,
  variable TEXT NOT NULL,
  value DOUBLE PRECISION NOT NULL,
  unit TEXT NOT NULL,
  native_resolution TEXT,
  quality_flag TEXT NOT NULL DEFAULT 'ok',
  PRIMARY KEY (forcing_version_id, station_id, variable, valid_time)
);
SELECT create_hypertable('met.forcing_station_timeseries', 'valid_time', if_not_exists => TRUE);

CREATE TABLE IF NOT EXISTS met.best_available_selection (
  selection_id BIGSERIAL,
  valid_time TIMESTAMPTZ NOT NULL,
  variable TEXT NOT NULL,
  selected_source TEXT NOT NULL,
  source_cycle_time TIMESTAMPTZ NOT NULL,
  fallback_order TEXT[] NOT NULL,
  quality_flag TEXT NOT NULL DEFAULT 'best_available_realtime',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (selection_id, valid_time),
  UNIQUE (valid_time, variable)
);
SELECT create_hypertable('met.best_available_selection', 'valid_time', if_not_exists => TRUE);
