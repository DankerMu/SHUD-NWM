## Why

node-27 磁盘上 `/home/ghdc/nwm/Basins/<basin>/forcing/` 已经有 8 个 basin 的 CMFD 0.1° per-grid-cell 静态历史气象数据（共约 4,224 个 grid CSV 文件，时间跨度 1951–2025 / 1979–2018 等），但 DB `met.*` schema 当前状态：

- `met.met_station`：只有 heihe (1709) + qhh (386) 两个 basin 有站点登记；其他 6 个有 CSV 的 basin（weiganhe / xinanjiang_upstream / kashigeer / keliya / hetianhe / qinyijiang）站点表 0 行。
- `met.forcing_version` / `met.forcing_station_timeseries`：10/10 basin 全部为空，包括 heihe + qhh —— 即使有站点也没 timeseries。
- `met.data_source`：仅 `gfs` (mock) + `IFS` (enabled)，**无 `cmfd` 行**。

后果：`apps/api/routes/data_sources.py:111` 的 `GET /api/v1/met/stations/{id}/series` API 是真实现而非 stub，但因为 `met.forcing_version` 表里没有任何 CMFD 行，任何 series 查询都 deterministic 返回 `FORCING_VERSION_NOT_FOUND`；地图上的 met-station MVT 图层（`apps/api/routes/flood_alerts.py:1210`）只对 heihe+qhh 有点位、其他 basin 全空。**当前前端 `/meteorology` 页面无法消费 CMFD source**：`apps/frontend/src/lib/hydroMet/queryState.ts:1` 的 `HydroMetSource` 类型硬编码为 `'GFS' | 'IFS'`，且 `packages/common/forecast_store.py:18` 的 `QHH_LATEST_SUPPORTED_SOURCES = ("GFS", "IFS")` 拒绝 CMFD source 参数 —— 也就是说，**仅 ingest DB 不足以让 `/meteorology` 自动出图**；前端 UI 暴露 CMFD 是独立的后续工作（明确 out of scope，本 change 不做）。用户在第二个 issue 明确要求把节点上实际气象数据接进来，本 change 解决"数据接入 DB + API 返回真数据 + curl/dev tools 可验"这一层；前端 UI 暴露由后续 issue 跟踪。

此外：现有 `workers/forcing_producer/` 走的是 GFS/IFS/ERA5 forecast 链路 —— canonical_met_product (grid registry) → IDW 插值 → station timeseries，**不直接处理静态历史 CSV**；`workers/model_registry/qhh_production_bootstrap.py` 是 QHH 专用，写 `met.met_station` 但不写 forcing timeseries。两条现有路径都不覆盖 CMFD CSV 直读场景。

## What Changes

- **新增** `met.data_source` 行：`source_id='cmfd'`, `source_type='archive_static'`, `status='enabled'`, `adapter_name='cmfd_csv_adapter'`（通过新 DB migration）。
- **新增** 6 个 basin 的 `met.met_station` 站点登记：从 `/home/ghdc/nwm/Basins/<basin>/forcing/X<lon>Y<lat>.csv` 文件名反推 station_id + geom（SRID 4490 Point）+ `properties_json.forcing_filename`；`station_role='forcing_grid'`, `active_flag=true`。覆盖：weiganhe / xinanjiang_upstream / kashigeer / keliya / hetianhe / qinyijiang。
- **新增** 8 个 basin 的 `met.forcing_version` + `met.forcing_station_timeseries` 写入（包含 heihe + qhh）：
  - 每个 basin 一行 `forcing_version`，`source_id='cmfd'`，`cycle_time=<CSV header start_date 转 TIMESTAMPTZ>`（CMFD 是静态数据集，cycle_time 合成为 `19510101T000000Z` 或 basin 各自的 CSV 起始时间）。`forcing_version_id` 格式：`forc_cmfd_<YYYYMMDD>_<model_id>`。
  - 每个 basin 的全量 timeseries 写入 hypertable：`(forcing_version_id, station_id, variable, valid_time)` 为 PK。预估写入量级 6.5 亿行（数量级，按 basin 站点数 × 时间步数累加；最大单 basin heihe = 1709 × 216224 × 5 vars ≈ 18.5 亿，按变量与时间分批写入）。
- **新增** worker 模块 `workers/cmfd_csv_ingest/`：
  - `parser.py` —— 解析 CMFD CSV 文件格式（header line 1 `<rows>\t<n_vars>\t<start_YYYYMMDD>\t<end_YYYYMMDD>\t86400`、header line 2 列名、数据行 `time_interval Precip_mm.d Temp_C RH_1 Wind_m.s RN_w.m2`）。
  - `station_seeder.py` —— 扫 basin forcing dir、生成 `met.met_station` upsert rows。
  - `timeseries_ingester.py` —— 调 parser，转换 `time_interval`（days from start）→ `valid_time` (TIMESTAMPTZ)，按 batch 写入 hypertable。
  - `cli.py` —— 单 basin 入口。
- **新增** orchestration script `scripts/ingest_cmfd_forcing_all_basins.py`：扫 `/home/ghdc/nwm/Basins/` 8 个有 CSV 的 basin，对每个 basin 调 station_seeder（若缺）+ timeseries_ingester，emit aggregate receipt。
- **变量映射**（CMFD CSV → API 变量名）：`Precip_mm.d → PRCP` (mm/day), `Temp_C → TEMP` (°C), `RH_1 → RH` (fraction 0-1), `Wind_m.s → wind` (m/s), `RN_w.m2 → Rn` (W/m²)。
- **OUT OF SCOPE（明确不做）**：
  - `tailanhe` + `zhaochen` (×4 variants) basin —— 磁盘 `/home/ghdc/nwm/Basins/<basin>/forcing/` 目录不存在，无 CMFD 数据可 ingest。会在 receipt 中显式标 `skipped: no_forcing_dir`。
  - `Press` 变量 —— CMFD CSV 只有 5 变量（无气压），API 当前 6 变量集合中的 Press 不补；API 对 `variables=Press` 返回空 series entry（既有契约不报错）。
  - canonical_met_product 链路 —— 直读 CSV，不走 `workers/forcing_producer/` 的 IDW 插值路径；不在 `met.canonical_met_product` 写 grid 元数据行（CMFD 是单一 grid，无需多源对齐）。
  - `met.interp_weight` —— 不写（CMFD 是 1:1 station↔grid_cell，weight 隐式为 1.0；现有 API series 查询不读这张表）。
  - `met.forcing_version_component` —— 不写（CMFD 不走 canonical → forcing 组合链路；该表是 forcing_version ↔ canonical_met_product 的 junction，CMFD 没有 canonical_met_product 行可挂）。
  - `Prcp_Correction.csv` 二次修正 —— phase 2 再考虑；本次 ingest 原始 `Precip_mm.d`。
  - API 代码改动 —— 现有 3 个 met 端点（stations / series / mvt-tiles）契约已对，不动代码。
  - **前端 UI 暴露 CMFD** —— frontend 当前硬编码 `HydroMetSource = 'GFS' | 'IFS'` + `latest-product` API 拒 CMFD source（`forecast_store.py:18`），即使 DB 有 CMFD 数据，`/meteorology` 页面也无法选 CMFD 渲染。前端扩展（新增 source 选择器 + bootstrap 分支跳过 latest-product / 直接调 `/met/stations`）是独立 issue，本 change **不做**。
  - **NaN/Inf/缺测值不写 DB** —— `met.forcing_station_timeseries.value DOUBLE PRECISION NOT NULL`（schema 强约束）；parser 检测到 NaN/Inf/空 cell 时 skip 该 (station × variable × valid_time) 行不入库（不写 NULL，也不写 sentinel），receipt 计数 `skipped_missing_count`。

## Capabilities

### New Capabilities
- `cmfd-csv-forcing-ingest`: 端到端 CMFD CSV → DB ingest 能力，包括 source 注册、station seeding、forcing_version + timeseries 写入、批 orchestration 与 receipt。

### Modified Capabilities
（无 —— 既有 spec 行为不变；API 与前端契约不动。）

## Impact

**新增代码：**
- `workers/cmfd_csv_ingest/__init__.py`
- `workers/cmfd_csv_ingest/parser.py` —— CMFD CSV 解析（header + 数据行 + 单位/时间换算）
- `workers/cmfd_csv_ingest/station_seeder.py` —— 从 forcing dir 文件名生成 station 行
- `workers/cmfd_csv_ingest/timeseries_ingester.py` —— forcing_version upsert + hypertable batch insert
- `workers/cmfd_csv_ingest/cli.py` —— 单 basin CLI
- `scripts/ingest_cmfd_forcing_all_basins.py` —— 批 orchestrator + receipt
- `tests/test_cmfd_csv_ingest_parser.py`（flat layout，与 `tests/test_forcing_producer.py` 一致；本仓库无 `tests/workers/` 子目录）
- `tests/test_cmfd_csv_ingest_station_seeder.py`
- `tests/test_cmfd_csv_ingest_station_seeder_real_db.py` (real-DB marker)
- `tests/test_cmfd_csv_ingest_timeseries_ingester.py`
- `tests/test_cmfd_csv_ingest_timeseries_ingester_real_db.py` (real-DB marker)
- `tests/test_cmfd_csv_ingest_orchestrator_smoke.py`

**新增 DB migration：**
- `db/migrations/000040_seed_cmfd_data_source.sql`（下一可用序号；若有竞争 PR 占用 000040，bump 至 000041）—— INSERT `met.data_source` 行 `('cmfd', 'CMFD 0.1° static historical forcing', 'archive_static', 'enabled', 'csv', NULL, 'cmfd_csv_adapter', '{"grid_resolution_deg": 0.1, "time_step_seconds": 10800, "variables": ["PRCP","TEMP","RH","wind","Rn"], "source_citation": "Yang et al. 2010"}'::jsonb)`。Idempotent（`ON CONFLICT (source_id) DO NOTHING`）。config_json 字段是 normative spec（见 spec.md "CMFD Data Source Registration" 要求）。

**不改动：**
- `apps/api/routes/data_sources.py` —— met API 已是真实现。
- `apps/api/routes/flood_alerts.py` met-stations MVT tile —— 已对，依赖 `met.met_station` 数据。
- `apps/frontend/src/pages/hydroMet/bootstrap.ts` —— 已能 graceful 处理空响应。
- `workers/forcing_producer/*` —— GFS/IFS/ERA5 路径不动；CMFD 走平行的新 worker。
- `workers/model_registry/qhh_production_bootstrap.py` —— heihe + qhh 站点表保持不动（既存 1709 + 386 行不删不动），CMFD ingest 写 timeseries 时 station_id 必须能在 `met.met_station` 查到（heihe + qhh 走既存路径，6 个新 basin 走 station_seeder）。

**DB 体量影响：**
- `met.data_source`: +1 行
- `met.met_station`: +约 1,529 行（6 basin 站点总和：401+50+372+32+581+93）
- `met.forcing_version`: +8 行
- `met.forcing_station_timeseries`: 数量级 ~6.5 亿行（hypertable，按 chunk 自动分区；hetianhe 32144 × 581 × 5 ≈ 9300 万，heihe 216224 × 1709 × 5 ≈ 18.5 亿，按变量 + 时间分批写）。需要按 basin 串行 ingest + 评估磁盘 + chunk 维持策略。**风险**：3 倍当前 DB 数据量；详见 design.md 风险章节。

**运维影响：**
- node-27 上执行 ingest（数据源在 node-27 `/home/ghdc/nwm/Basins/`，DB 也在 node-27）。
- 单 basin ingest 预估 30 分钟–几小时（取决于站点 × 时间步），全 8 basin 数小时到一天，按 basin 串行。
- 一旦 ingest 完成，`/api/v1/met/stations/{id}/series?source_id=cmfd&cycle_time=<basin start_date>&model_id=basins_<basin>_shud` 直接返回真数据（curl + JSON 验证）；MVT met-stations 瓦片对 6 个新 basin 出点位（地图 dev tools 看 pbf 二进制）。**前端 `/meteorology` 页面 UI 不会自动渲染 CMFD source**，需要后续 issue 扩展 frontend HydroMetSource 类型 + bootstrap 分支才能在浏览器中可视化 CMFD 时间序列；本 change 完成后 API 已准备就绪，前端工作可独立推进。
- 后续若 CMFD 上游数据更新（新加年份），ingest 是幂等的 —— 同 `forcing_version_id` UPSERT，时间序列按 `(forcing_version_id, station_id, variable, valid_time)` PK 去重；但**重新 ingest 整条 forcing_version 需要走 `replace_forcing_timeseries()` 的 DELETE+INSERT 路径**。

**测试影响：**
- 新增 unit 测试 + real-DB integration 测试（node-27 跑 pytest 真实 hypertable 写入 + 查询 round-trip）。
- M11Shell mock fixture 已经覆盖 met endpoints 返回正常 station 列表；无需改 frontend 测试。

**Doc 影响：**
- 新增 `docs/runbooks/ingest-cmfd-forcing.md`（运维手册）
- 新增 `docs/architecture/cmfd-csv-ingest.md`（架构说明）
- 更新 `docs/data-pipelines.md`（如有）说明 CMFD 旁路链路
