# 03. 数据库设计

版本：v0.2  
日期：2026-05-06

## 1. 数据库选型

```text
PostgreSQL + PostGIS：空间对象、元数据、模型版本、河网版本、频率曲线。
TimescaleDB：高频 forcing、河段时序、重现期时序。
Object Storage：原始资料、canonical 产品、SHUD 输入输出、日志、瓦片。
```

## 2. Schema 分区

```text
core       系统核心对象、流域、模型、版本
met        气象资料源、周期、canonical 产品、forcing
hydro      SHUD 运行、状态快照、河段结果
flood      频率曲线和重现期结果
map        瓦片发布、图层、样式
ops        作业、日志、质量控制、审计
```

## 3. 核心实体关系

```mermaid
erDiagram
  basin ||--o{ basin_version : has
  basin_version ||--o{ river_network_version : has
  basin_version ||--o{ met_station : defines
  river_network_version ||--o{ river_segment : contains
  basin_version ||--o{ model_instance : used_by
  model_instance ||--o{ state_snapshot : produces
  model_instance ||--o{ hydro_run : runs
  hydro_run ||--o{ river_timeseries : outputs
  hydro_run ||--o{ return_period_result : generates
  model_instance ||--o{ flood_frequency_curve : calibrates
  data_source ||--o{ forecast_cycle : publishes
  forecast_cycle ||--o{ canonical_met_product : converts
  canonical_met_product ||--o{ forcing_version : derives
  forcing_version ||--o{ hydro_run : drives
  forcing_version ||--o{ forcing_station_timeseries : contains
  forcing_version ||--o{ forcing_version_component : composed_of
  canonical_met_product ||--o{ forcing_version_component : used_in
  met_station ||--o{ interp_weight : weighted_by
  met_station ||--o{ forcing_station_timeseries : records
```

## 4. 状态枚举类型

> 关键状态字段必须使用 ENUM 约束，避免业务运行后出现 `success`/`succeeded`/`done`/`complete` 等混用。

```sql
CREATE TYPE hydro.run_type AS ENUM (
  'analysis',
  'forecast',
  'hindcast'
);

CREATE TYPE hydro.run_status AS ENUM (
  'created',
  'staged',
  'submitted',
  'running',
  'succeeded',
  'parsed',
  'frequency_done',
  'published',
  'failed',
  'cancelled',
  'superseded'
);

CREATE TYPE met.source_status AS ENUM (
  'enabled',
  'restricted',
  'planned',
  'mock',
  'deprecated'
);

CREATE TYPE met.cycle_status AS ENUM (
  'discovered',
  'downloading',
  'raw_complete',
  'canonical_ready',
  'forcing_ready_partial',
  'forcing_ready',
  'forecast_running',
  'parsed_partial',
  'complete',
  'published',
  'failed_download',
  'failed_convert',
  'failed_forcing',
  'failed_run',
  'failed_parse',
  'failed_publish'
);
```

## 5. 关键表定义

> 建表顺序按外键依赖排列。表编号按 `{schema}.{逻辑顺序}` 连续编排。

### 5.1 `core.basin`

```sql
CREATE TABLE core.basin (
  basin_id TEXT PRIMARY KEY,
  basin_name TEXT NOT NULL,
  basin_group TEXT,
  description TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

### 5.2 `core.basin_version`

```sql
CREATE TABLE core.basin_version (
  basin_version_id TEXT PRIMARY KEY,
  basin_id TEXT NOT NULL REFERENCES core.basin(basin_id),
  version_label TEXT NOT NULL,
  geom geometry(MultiPolygon, 4490) NOT NULL,
  active_flag BOOLEAN NOT NULL DEFAULT false,
  valid_from TIMESTAMPTZ,
  valid_to TIMESTAMPTZ,
  source_uri TEXT,
  checksum TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX basin_version_geom_gix ON core.basin_version USING gist (geom);
```

> **`active_flag` 权威归属（#1695）**：`core.basin_version.active_flag`
> 对**计算面不承载任何权威**；在**展示面它是「默认选中版本」的选择器**。
>
> 写入侧：Basins importer 对它创建的每一行硬编码写 `false`
> （`workers/model_registry/basins_registry_import.py:542-548`），
> 此后没有任何 `UPDATE` 路径碰这一列——importer 后续的
> `UPDATE core.basin_version`（`basins_registry_import.py:799`）只改
> `source_uri` / `checksum`。两个内部写 API（`POST /api/v1/basins`
> → `::create_basin_with_version`，`POST /api/v1/basins/{basin_id}/versions`
> → `::create_basin_version`，都落到
> `packages/common/model_registry.py::_insert_basin_version`，`:2549` 绑定该字段）
> 确实接受 payload 里的 `active_flag`，可以在**新建行**上把它写成 `true`，
> 但生产 ingest 不走这条路（现有行全部来自 Basins importer）。
>
> 读者集合（非测试，2026-09-02 全仓核查）：
>
> - **后端排序 tiebreak**：`packages/common/model_registry.py:874` 的
>   `ORDER BY active_flag DESC, created_at DESC, basin_version_id`。
> - **API 透传**：同一查询把该列 SELECT 出来（`:866`），
>   `_basin_version_public_projection`（`:3611-3617`）只抹 `source_uri`/`checksum`、
>   保留 `active_flag`，于是它经
>   `GET /api/v1/basins/{basin_id}/versions`（`apps/api/routes/models.py:381-393`）出网。
> - **前端默认版本选择**：`fetchBasinVersions`
>   （`apps/frontend/src/stores/overviewData.ts:543-551`）→ `normalizeBasinVersions`
>   （`apps/frontend/src/lib/m11/overviewDataContracts.ts:846-854`，
>   `active: version.active_flag`）→
>   `versions.find(v => v.active_flag)`（`overviewData.ts:1285`）与
>   `versionOptions.find(v => v.active)`（`overviewDataContracts.ts:396`、`:601`）。
>   它决定 `selectedBasinVersionId`，进而决定 basin 的 boundary / bbox / areaKm2，
>   在 basin detail 里还决定 models 过滤（`overviewDataContracts.ts:605`）
>   与由此得出的 `activeModelCount`（`:617`），以及按 basin_version 发起的下游请求。
>   （`createBasinSummaries` 的 `riverCount`/`activeModelCount`（`:407-408`）按
>   basin 的全部版本聚合，不受这个选择器影响。）
>
> node-27 实测（2026-09-02，只读）：`core.basin_version` 44 行中 `active_flag = true` 的有 **0** 行。
> 全表皆 `false` 时上述选择器全部退化为 no-op（回落到列表首行），
> 所以它**今天**不改变任何行为；但把任意一行置 `true`
> 会改变该 basin 的默认选中版本及上面列出的下游值——它不是死列，只是当前处于 no-op 状态。
> 另外，「查库看到 `active_flag` 全 false」**不等于**「没有模型在跑」——
> 计算面的权威是 node-22 file-registry manifest（见下面 §5.5 的注记）。
> 要让某个 basin version 退出展示列表，改 `valid_to`，不要改 `active_flag`。

### 5.3 `core.river_network_version`

```sql
CREATE TABLE core.river_network_version (
  river_network_version_id TEXT PRIMARY KEY,
  basin_version_id TEXT NOT NULL REFERENCES core.basin_version(basin_version_id),
  version_label TEXT NOT NULL,
  segment_count INT NOT NULL,
  source_uri TEXT,
  checksum TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

### 5.4 `core.river_segment`

```sql
CREATE TABLE core.river_segment (
  river_segment_id TEXT NOT NULL,
  river_network_version_id TEXT NOT NULL REFERENCES core.river_network_version(river_network_version_id),
  segment_order INT,
  downstream_segment_id TEXT,
  length_m DOUBLE PRECISION,
  geom geometry(MultiLineString, 4490),
  properties_json JSONB NOT NULL DEFAULT '{}',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (river_segment_id, river_network_version_id)
);
CREATE INDEX river_segment_geom_gix ON core.river_segment USING gist (geom);
```

> `geom` 列在 migration 000037 升为 `MultiLineString(4490)`；PR-2 (issue #561) 之后所有写入的 reach 行 holds exactly one part（来源于 `gis/river.shp` 的单 part flow-ordered polyline），wrapper 类型保留是为了将来某 basin 的源数据若真正需要 multi-part reach 时不必再做 schema 变更。

### 5.5 `core.model_instance`

```sql
CREATE TABLE core.model_instance (
  model_id TEXT PRIMARY KEY,
  basin_version_id TEXT NOT NULL REFERENCES core.basin_version(basin_version_id),
  river_network_version_id TEXT NOT NULL REFERENCES core.river_network_version(river_network_version_id),
  mesh_version_id TEXT NOT NULL,
  calibration_version_id TEXT NOT NULL,
  shud_code_version TEXT NOT NULL,
  rshud_code_version TEXT,
  autoshud_code_version TEXT,
  container_image TEXT,
  model_package_uri TEXT NOT NULL,
  active_flag BOOLEAN NOT NULL DEFAULT false,
  resource_profile JSONB NOT NULL DEFAULT '{}',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

> **`active_flag` 权威归属（#1695）**：`core.model_instance.active_flag` 是
> **展示面与 lifecycle 的权威**；对生产计算面（file lane，
> `NHMS_SCHEDULER_REGISTRY_BACKEND=file`）它没有权威，权威是 manifest；只有在
> postgres lane（代码默认值 `postgres`，生产未用）它才是调度 run/no-run 的闸门。
> 以下非 test 读者按面分组列出
> （grep 口径：`grep -rn active_flag packages services workers scripts apps db`，
> 剔除同名但不同表的列——`core.basin_version` / `met.met_station` /
> `met.interp_weight`——和纯写入点
> （INSERT 字面量、schema DDL、API payload 直通），2026-09-02）。列出它们是为了
> 能看清翻这个 flag 的爆炸半径；写这一列的路径除下文的激活闸门外还有
> `workers/model_registry/qhh_production_bootstrap.py::_activate_qhh_model`（`:1622`）
> 与 `:1675-1679` 的持久化置 inactive，不在读者清单内：
>
> - **展示成员判定**：全国 river-network MVT（`services/tiles/mvt.py:367`，
>   另见 `:442`、`:653`、`:691`、`:1411`）；
> - **展示 source-version 摘要**：`services/tiles/mvt.py:1426`
>   （`national_river_network_source_version`，方言相关谓词）、`:1590`
>   （`national_discharge_valid_times`，函数头 `:1544`）；
> - **前端**：`activeModelCount`（`apps/frontend/src/lib/m11/overviewDataContracts.ts:408`、`:617`）、
>   ops 侧 model-asset 过滤器（`apps/frontend/src/stores/modelAssets.ts:637`）、
>   model-assets 页的 supersede 候选筛选与启用/停用徽章
>   （`apps/frontend/src/pages/ModelAssetsPage.tsx:79`、`:346`）；
> - **ingest 面的行为读者**（影响的不只是展示）：
>   `workers/model_registry/basins_registry_import.py::_model_active_state`
>   （`:2363-2373`，在 `:352` 决定重复导入时报告的 active 状态）、
>   `workers/model_registry/qhh_production_bootstrap.py:1168-1182`（bootstrap 的
>   `SELECT … FOR UPDATE` 现状读）、`:1741`（同 basin 的重复 active 行检测）、
>   `:1791-1803`（`_fetch_model_identity`，回填 `:1101` 的 `active` 字段）；
> - **激活闸门**：`scripts/node27_autopipeline.py:1255-1260`（详见下文）；
> - **调度 run/no-run 判定**：`services/orchestrator/scheduler_models.py::coerce_registered_model`
>   （`:128` 由该 flag 推导 `lifecycle_state`，`:137-138` 在 flag 为 `False`
>   或 state 非 `active` 时返回 `model_exclusion(row, "inactive_model")`，即该 model
>   本轮不跑）；它的入参行由 `fetch_active_model_details`（`:29`）备好——
>   候选集先由 `registry.list_models(..., active=True, ...)`（`:41`）圈定，
>   逐条再经 `fetch_scheduler_model_detail`（`:50`，实现在 `:58-62`，
>   走 `get_model_internal` / `get_model`）取回明细。postgres backend 下这个
>   registry 是 `packages/common/model_registry.py::PsycopgModelRegistryStore`
>   （`:580`），`:41` 的 `active=True` 落到 `list_models`（`:2372`）的
>   `active_flag = %s` 过滤子句（`:2386`，`:2402` 把该谓词按 JOIN 重限定为
>   `mi.active_flag`）；file provider 下则来自 manifest 行（见下文计算面段落）；
> - **model lifecycle API**：`packages/common/model_registry.py`（多处，读写兼有）。
>
> API 层的 payload / schema 字段（`apps/api/main.py:154`、
> `apps/api/routes/models.py:46`、`:110`、`:135`、`apps/api/openapi_patching.py:1403`、
> `apps/frontend/src/api/types.ts`）属于 lifecycle API 契约本身，不另列。
>
> **计算面的权威是 node-22 的 file-registry manifest**
> （`manifest-last.json`，由 `scripts/publish_scheduler_file_registry.py` 写出，
> 其中每个 model 带 `active_flag` / `lifecycle_state` 字段；file provider 从 manifest 行读它，
> 见 `services/orchestrator/scheduler_file_providers.py:156`、`:162`、`:1106`）。
> 生产调度器在 `NHMS_SCHEDULER_REGISTRY_BACKEND=file` 下是 DB-free 的
> （env 文件 `compute.scheduler-dbfree.env`，node-22 不连活 DB），
> 因此**两个 DB flag 它都够不着**；会读 DB 的那条路
> （`services/orchestrator/chain_repository.py:265`，读 `core.model_instance`）属于 postgres backend。
>
> 两个面**按设计不同步**。所以会出现这个看起来矛盾的实测组合
> （node-27，2026-09-02，只读）：baseline `basins_*_shud` 行 **38 true / 6 false**，
> 而真正在 node-22 上跑的 `dg_*` 行 **0 true / 153 false**。
> `dg_*` 保持 false 的可追溯原因是 node-27 autopipeline 的激活函数
> `scripts/node27_autopipeline.py::_activate_model`（`:1235-1266`，语句为
> `UPDATE core.model_instance SET active_flag = true, lifecycle_state = 'active'`；
> 调用点 `:1300`、`:1947`）自带 one-active-sibling 闸门：对尚未 active 的行，
> 只有当同一 `basin_version_id` 下没有其它 active 行时才会更新（`:1255-1260`
> 的 `active_flag = true OR NOT EXISTS (…)` 谓词——已 active 的行是幂等分支），
> 撞上 baseline 兄弟行的 variant 拿到 `rowcount == 0`；
> 该谓词的形状由 `tests/test_node27_autopipeline_preflight.py:832-869` 钉住
> （mock cursor 上断言 SQL 含 `NOT EXISTS` 与
> `active_sibling.basin_version_id = core.model_instance.basin_version_id`；
> `:864` 的 `== 0` 来自 mock 的固定 `rowcount`，不是真库行为，真库下的
> rowcount 无测试覆盖）。生产（file lane）调度器不读这个列，
> 所以它们照跑不误。
>
> 不要为了「让数字好看」去翻 `dg_*` 的 flag。两道 DB 闸门会先拦住你：
> 只改 `active_flag` 会被 CHECK 约束
> `model_instance_active_lifecycle_consistency_chk` 拒绝
> （`db/migrations/000022_model_asset_lifecycle.sql:42-46`，要求 `active_flag = true`
> 与 `lifecycle_state = 'active'` 同时成立；`lifecycle_state` 是
> `TEXT NOT NULL DEFAULT 'inactive'`（同文件 `:1-2`），所以没有 NULL 行让
> CHECK 落空）；两列一起改、而该
> `basin_version_id` 已有 active 行时，会被 partial UNIQUE
> `model_instance_active_basin_version_uidx` 拒绝（同文件 `:61-63`）。
> **只有**当目标行所属 `basin_version` 没有 active 兄弟行时这个翻转才会落库，
> 那时它**会**改变全国 MVT 的展示成员；这类 basin_version 确实存在
> （同一次只读实测里 6 个 baseline 行为 false，但未逐行核对它们与 `dg_*`
> 行的 basin_version 对应关系）。真正危险的动作是为了绕开这两道约束而去
> 松 `lifecycle_state` 或改约束本身。
> 另：`services/tiles/mvt.py:367` 的成员谓词里没有 baseline/variant 判别，
> 「展示成员只认 baseline 行上的这个 flag」是当前数据状态下的观察
> （只有 baseline 行是 true），不是查询语义。
> `core.basin_version.active_flag` 的（无）权威见 [§5.2 `core.basin_version` 的注记](#52-corebasin_version)。

### 5.6 `met.data_source`

```sql
CREATE TABLE met.data_source (
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
```

### 5.7 `met.forecast_cycle`

```sql
CREATE TABLE met.forecast_cycle (
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
```

### 5.8 `met.canonical_met_product`

```sql
CREATE TABLE met.canonical_met_product (
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
CREATE INDEX canonical_met_source_cycle_idx ON met.canonical_met_product (source_id, cycle_time, variable);
```

### 5.9 `met.met_station`

> 气象代站是流域地理实体，与 `basin_version` 绑定，不与单个 `model_instance` 耦合。多个模型版本可共享同一组代站。模型-特定的格点权重通过 `met.interp_weight` 关联。

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
CREATE INDEX met_station_basin_idx ON met.met_station (basin_version_id);
```

### 5.10 `met.interp_weight`

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

### 5.11 `met.forcing_version`

```sql
CREATE TABLE met.forcing_version (
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
```

### 5.11b `met.forcing_version_component`

> 显式记录每个 forcing_version 由哪些 canonical_met_product 组成，用于血缘查询和审计，避免依赖 JSON 解析。

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

### 5.12 `met.forcing_station_timeseries`

> 气象代站 forcing 时序是前端代站曲线、forcing 追溯和 SHUD 输入审计的核心表。

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

### 5.13 `met.best_available_selection`

> v1 规则采用全域选择：每个时刻每个变量全系统统一选择一个 source。如果后续需要空间分区混合（如 CLDAS 仅覆盖中国区域），可升级为 `UNIQUE (valid_time, variable, domain_id)` 或 grid-cell 级 lineage。当前设计满足 MVP 需求。

```sql
CREATE TABLE met.best_available_selection (
  selection_id BIGSERIAL PRIMARY KEY,
  valid_time TIMESTAMPTZ NOT NULL,
  variable TEXT NOT NULL,
  selected_source TEXT NOT NULL,
  source_cycle_time TIMESTAMPTZ NOT NULL,
  fallback_order TEXT[] NOT NULL,
  quality_flag TEXT NOT NULL DEFAULT 'best_available_realtime',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (valid_time, variable)
);
SELECT create_hypertable('met.best_available_selection', 'valid_time', if_not_exists => TRUE);
```

### 5.14 `hydro.hydro_run`

```sql
CREATE TABLE hydro.hydro_run (
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
```

> `init_state_id` 暂保留为 TEXT 不加外键，因为 `state_snapshot` 与 `hydro_run` 存在循环依赖（run 产生 state，state 又作为下一 run 的 init）。可通过后置 `ALTER TABLE` 或应用层约束保证引用完整性。

### 5.15 `hydro.state_snapshot`

```sql
CREATE TABLE hydro.state_snapshot (
  state_id TEXT PRIMARY KEY,
  model_id TEXT NOT NULL REFERENCES core.model_instance(model_id),
  run_id TEXT NOT NULL REFERENCES hydro.hydro_run(run_id),
  valid_time TIMESTAMPTZ NOT NULL,
  state_uri TEXT NOT NULL,
  checksum TEXT NOT NULL,
  usable_flag BOOLEAN NOT NULL DEFAULT false,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (model_id, valid_time)
);
```

### 5.16 `hydro.river_timeseries`

> `river_segment` 的主键是 `(river_segment_id, river_network_version_id)` 复合键，因此 `river_timeseries` 必须同时保存 `river_network_version_id`，否则跨河网版本升级后会产生歧义。主键也纳入 `river_network_version_id` 以保持自洽。

```sql
CREATE TABLE hydro.river_timeseries (
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
CREATE INDEX river_ts_segment_time_idx ON hydro.river_timeseries (river_segment_id, variable, valid_time DESC);
```

### 5.17 `flood.flood_frequency_curve`

```sql
CREATE TABLE flood.flood_frequency_curve (
  curve_id TEXT PRIMARY KEY,
  model_id TEXT NOT NULL REFERENCES core.model_instance(model_id),
  river_network_version_id TEXT NOT NULL,
  basin_version_id TEXT NOT NULL,
  river_segment_id TEXT NOT NULL,
  duration TEXT NOT NULL,
  method TEXT NOT NULL,
  sample_period_start DATE NOT NULL,
  sample_period_end DATE NOT NULL,
  sample_size INT NOT NULL,
  parameters_json JSONB NOT NULL,
  q2 DOUBLE PRECISION,
  q5 DOUBLE PRECISION,
  q10 DOUBLE PRECISION,
  q20 DOUBLE PRECISION,
  q50 DOUBLE PRECISION,
  q100 DOUBLE PRECISION,
  unit TEXT NOT NULL DEFAULT 'm3/s',
  quality_flag TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (model_id, river_network_version_id, river_segment_id, duration, method, sample_period_start, sample_period_end)
);
```

### 5.18 `flood.return_period_result`

> 前端洪水预警图层和预警聚合 API 的核心产品表。除原始 run 关联外，补充版本和来源字段以支持跨版本追溯和瓦片发布。

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

### 5.19 `map.tile_layer`

```sql
CREATE TABLE map.tile_layer (
  layer_id TEXT PRIMARY KEY,
  layer_type TEXT NOT NULL,
  source_run_id TEXT,
  source_product_id TEXT,
  variable TEXT,
  valid_time TIMESTAMPTZ,
  tile_format TEXT NOT NULL,
  tile_uri_template TEXT NOT NULL,
  min_zoom INT NOT NULL DEFAULT 0,
  max_zoom INT NOT NULL DEFAULT 14,
  style_json JSONB,
  published_flag BOOLEAN NOT NULL DEFAULT false,
  publish_time TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

### 5.20 `map.tile_cache`

```sql
CREATE TABLE map.tile_cache (
  layer_id TEXT NOT NULL REFERENCES map.tile_layer(layer_id),
  z INT NOT NULL,
  x INT NOT NULL,
  y INT NOT NULL,
  tile_data BYTEA,
  tile_uri TEXT,
  etag TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (layer_id, z, x, y)
);
```

## 6. 查询模式

### 6.1 点击河段曲线

```sql
SELECT valid_time, scenario_id, variable, value, unit
FROM hydro.river_timeseries rt
JOIN hydro.hydro_run hr ON rt.run_id = hr.run_id
WHERE rt.river_segment_id = :segment_id
  AND rt.variable = 'q_down'
  AND hr.run_id IN (:analysis_run_id, :gfs_run_id, :ifs_run_id)
ORDER BY valid_time;
```

### 6.2 获取河段重现期瓦片属性

```sql
SELECT river_segment_id, max(return_period) AS max_t
FROM flood.return_period_result
WHERE run_id = :run_id
  AND valid_time BETWEEN :start_time AND :end_time
GROUP BY river_segment_id;
```

## 7. 版本切换规则

- 新 model_instance 上线后，必须先 `active_flag=false`。
- 完成 smoke test、历史样本、频率曲线后，才可设为 active。
- 同一个 basin_version 可有多个 model_instance，但同一业务产品线只能有一个 active model。
- 旧模型不可删除，只能 deprecated。

## 8. 本文档与附录 C 的职责分工

```text
03_database_design.md（本文档）：
  实体关系、设计原则、完整表定义、枚举类型、查询模式、版本规则。
  作为数据库设计的权威来源。

C_database_schema_draft.md（附录 C）：
  接近 migration 的 SQL 草案，可包含索引优化、分区策略等实施细节。
  当两处不一致时，以本文档为准。
```
