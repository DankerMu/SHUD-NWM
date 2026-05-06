# 附录 C. 数据库 Schema 草案

版本：v0.2  
日期：2026-05-06

> 本附录为接近 migration 的 SQL 草案，可包含索引优化、分区策略等实施细节。当本附录与 `03_database_design.md`（主数据库设计）不一致时，以主设计文档为准。正式建表脚本应通过 migration 工具管理。

## 1. Schema 创建

```sql
CREATE SCHEMA IF NOT EXISTS core;
CREATE SCHEMA IF NOT EXISTS met;
CREATE SCHEMA IF NOT EXISTS hydro;
CREATE SCHEMA IF NOT EXISTS flood;
CREATE SCHEMA IF NOT EXISTS map;
CREATE SCHEMA IF NOT EXISTS ops;
```

## 2. 状态枚举类型

```sql
CREATE TYPE hydro.run_type AS ENUM (
  'analysis', 'forecast', 'hindcast'
);

CREATE TYPE hydro.run_status AS ENUM (
  'created', 'staged', 'submitted', 'running', 'succeeded',
  'parsed', 'frequency_done', 'published',
  'failed', 'cancelled', 'superseded'
);

CREATE TYPE met.source_status AS ENUM (
  'enabled', 'restricted', 'planned', 'mock', 'deprecated'
);

CREATE TYPE met.cycle_status AS ENUM (
  'discovered', 'downloading', 'raw_complete', 'canonical_ready',
  'forcing_ready_partial', 'forcing_ready', 'forecast_running',
  'parsed_partial', 'complete', 'published',
  'failed_download', 'failed_convert', 'failed_forcing',
  'failed_run', 'failed_parse', 'failed_publish'
);
```

## 3. 气象代站与插值权重表

```sql
CREATE TABLE met.met_station (
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
CREATE INDEX met_station_geom_gix ON met.met_station USING gist (geom);
```

```sql
CREATE TABLE met.interp_weight (
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
```

## 4. 时序表建议

```sql
CREATE TABLE met.forcing_station_timeseries (
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
```

```sql
CREATE TABLE flood.return_period_result (
  run_id TEXT NOT NULL REFERENCES hydro.hydro_run(run_id),
  scenario_id TEXT NOT NULL,
  basin_version_id TEXT NOT NULL,
  river_network_version_id TEXT NOT NULL,
  model_id TEXT NOT NULL,
  river_segment_id TEXT NOT NULL,
  valid_time TIMESTAMPTZ NOT NULL,
  duration TEXT NOT NULL,
  q_value DOUBLE PRECISION NOT NULL,
  q_unit TEXT NOT NULL DEFAULT 'm3/s',
  return_period DOUBLE PRECISION,
  warning_level TEXT,
  source_id TEXT,
  cycle_time TIMESTAMPTZ,
  max_over_window BOOLEAN DEFAULT false,
  quality_flag TEXT NOT NULL DEFAULT 'ok',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (run_id, river_segment_id, duration, valid_time)
);
SELECT create_hypertable('flood.return_period_result', 'valid_time', if_not_exists => TRUE);
```

## 4b. Forcing 血缘关系表

```sql
CREATE TABLE met.forcing_version_component (
  forcing_version_id TEXT NOT NULL REFERENCES met.forcing_version(forcing_version_id),
  canonical_product_id TEXT NOT NULL REFERENCES met.canonical_met_product(canonical_product_id),
  variable TEXT NOT NULL,
  valid_time_start TIMESTAMPTZ,
  valid_time_end TIMESTAMPTZ,
  role TEXT NOT NULL DEFAULT 'forcing_input',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (forcing_version_id, canonical_product_id, variable)
);
```

## 5. 运维表

> 统一使用 `ops.qc_result`（与 `07_devops_ops_security.md` 一致），不再保留 `ops.quality_check` 名称。

```sql
CREATE TABLE ops.pipeline_job (
  job_id TEXT PRIMARY KEY,
  run_id TEXT,
  cycle_id TEXT,
  job_type TEXT NOT NULL,
  slurm_job_id TEXT,
  status TEXT NOT NULL DEFAULT 'pending',
  stage TEXT,
  submitted_at TIMESTAMPTZ,
  started_at TIMESTAMPTZ,
  finished_at TIMESTAMPTZ,
  exit_code INT,
  retry_count INT NOT NULL DEFAULT 0,
  error_code TEXT,
  error_message TEXT,
  log_uri TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX pipeline_job_run_idx ON ops.pipeline_job (run_id);
CREATE INDEX pipeline_job_cycle_idx ON ops.pipeline_job (cycle_id);
```

```sql
CREATE TABLE ops.pipeline_event (
  event_id BIGSERIAL PRIMARY KEY,
  entity_type TEXT NOT NULL,
  entity_id TEXT NOT NULL,
  event_type TEXT NOT NULL,
  status_from TEXT,
  status_to TEXT,
  message TEXT,
  details JSONB NOT NULL DEFAULT '{}',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX pipeline_event_entity_idx ON ops.pipeline_event (entity_type, entity_id, created_at DESC);
```

```sql
CREATE TABLE ops.qc_result (
  qc_id BIGSERIAL PRIMARY KEY,
  qc_checkpoint TEXT NOT NULL,
  target_type TEXT NOT NULL,
  target_id TEXT NOT NULL,
  run_id TEXT,
  cycle_id TEXT,
  passed BOOLEAN NOT NULL,
  severity TEXT NOT NULL DEFAULT 'info',
  checks_json JSONB NOT NULL,
  message TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX qc_result_target_idx ON ops.qc_result (target_type, target_id, created_at DESC);
```

```sql
CREATE TABLE ops.audit_log (
  log_id BIGSERIAL PRIMARY KEY,
  actor TEXT NOT NULL,
  actor_role TEXT NOT NULL,
  action TEXT NOT NULL,
  entity_type TEXT NOT NULL,
  entity_id TEXT NOT NULL,
  details JSONB NOT NULL DEFAULT '{}',
  ip_address INET,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX audit_log_entity_idx ON ops.audit_log (entity_type, entity_id, created_at DESC);
CREATE INDEX audit_log_actor_idx ON ops.audit_log (actor, created_at DESC);
```
