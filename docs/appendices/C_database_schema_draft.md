# 附录 C. 数据库 Schema 草案

版本：v0.1  
日期：2026-04-30

> 本附录为开发初稿，正式建表脚本应通过 migration 工具管理。

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
  forcing_version_id TEXT NOT NULL,
  basin_version_id TEXT NOT NULL,
  station_id TEXT NOT NULL,
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
  run_id TEXT NOT NULL,
  scenario_id TEXT NOT NULL,
  river_segment_id TEXT NOT NULL,
  valid_time TIMESTAMPTZ NOT NULL,
  duration TEXT NOT NULL,
  q_value DOUBLE PRECISION NOT NULL,
  q_unit TEXT NOT NULL DEFAULT 'm3/s',
  return_period DOUBLE PRECISION,
  warning_level TEXT,
  quality_flag TEXT NOT NULL DEFAULT 'ok',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (run_id, river_segment_id, duration, valid_time)
);
SELECT create_hypertable('flood.return_period_result', 'valid_time', if_not_exists => TRUE);
```

## 5. 运维表建议

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
```

```sql
CREATE TABLE ops.quality_check (
  qc_id BIGSERIAL PRIMARY KEY,
  entity_type TEXT NOT NULL,
  entity_id TEXT NOT NULL,
  check_name TEXT NOT NULL,
  status TEXT NOT NULL,
  severity TEXT NOT NULL,
  metrics JSONB NOT NULL DEFAULT '{}',
  message TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
```
