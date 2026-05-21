# QHH 后端完整链路复测记录

最后更新：2026-05-21

## 目标

以 `data/Basins/qhh` 的已校准 SHUD 模型为首个真实流域资产，复测不含前端的后端功能链路：

1. Basins 资产发现、发布、registry 导入。
2. GFS 气象数据下载。
3. raw 到 canonical 转换。
4. forcing 生产。
5. 使用仓库内 `SHUD/shud` 运行 qhh 模型。
6. 输出解析、QC 与结果摘要。

本轮原则：不编译 SHUD，不使用 Docker；PostgreSQL、对象存储和运行产物尽量放在项目目录下，避免占用系统盘。

## 环境与入口

- Basins 根：`data/Basins -> /volume/data/nwm/Basins`
- 选定流域：`qhh`
- 模型 ID：`basins_qhh_shud`
- package version：`v0.0.1-qhh-smoke-lake2`
- 本地 run/object root：`.nhms-runs/qhh-smoke/`
- 本地 PostgreSQL 数据目录：`.pgdata/qhh-smoke`
- 本地 PostgreSQL runtime：`.conda-postgres-runtime`
- 本地 PostgreSQL URL：`postgresql://nhms:nhms_dev@127.0.0.1:55432/nhms`
- SHUD executable：`SHUD/shud`

启动或查看本地 PostgreSQL：

```bash
./scripts/local_pg.sh start
./scripts/local_pg.sh status
./scripts/local_pg.sh url
```

可重复执行完整链路：

```bash
export DATABASE_URL=$(./scripts/local_pg.sh url)
export QHH_CYCLE_TIME=2026052100
export QHH_RESET_SMOKE_DB=1
export SHUD_TIMEOUT_SECONDS=1800
./scripts/run_qhh_backend_smoke.sh
```

脚本默认设置：

- `QHH_PACKAGE_VERSION=v0.0.1-qhh-smoke-lake2`
- `QHH_GFS_FORECAST_START_HOUR=3`
- `QHH_GFS_FORECAST_END_HOUR=24`
- `QHH_MODEL_OUTPUT_INTERVAL=180`
- `QHH_SHUD_COMMAND_STYLE=shud_project`
- `SHUD_EXECUTABLE=$PWD/SHUD/shud`

## 本次闭环结果

脚本级完整复跑已在 2026-05-21 完成，最终 gate：

```json
{
  "status": "ready",
  "reason": "SHUD runtime and output parse completed."
}
```

关键产物：

```text
.nhms-runs/qhh-smoke/basins-inventory.json
.nhms-runs/qhh-smoke/qhh-package-manifest.json
.nhms-runs/qhh-smoke/qhh-registry-import-report.json
.nhms-runs/qhh-smoke/gfs-download.stdout.json
.nhms-runs/qhh-smoke/canonical-convert.stdout.json
.nhms-runs/qhh-smoke/forcing-produce.stdout.json
.nhms-runs/qhh-smoke/runs/qhh_gfs_2026052100_smoke/input/manifest.json
.nhms-runs/qhh-smoke/runs/qhh_gfs_2026052100_smoke/output/qhh.rivqdown.csv
.nhms-runs/qhh-smoke/qhh-result-summary.json
.nhms-runs/qhh-smoke/qhh-display-products.json
```

### Basins discovery 与 package

- discovery：成功，发现 13 个模型目录。
- `resolved_root=/volume/data/nwm/Basins`
- qhh package：`v0.0.1-qhh-smoke-lake2`
- package status：`already_done`
- package checksum：`9ae256969dd77a7a2966f2c47298a571c18d24c364e79ce43a5c6f81d8da26a3`
- included files：50
- optional lake runtime files：`qhh.lake.sp`、`qhh.lake.bathy`、`qhh.lake.ic`
- 历史 forcing CSV 仍按 package 策略 `excluded_by_default` 不复制 1.52GB payload。

### Registry 与 qhh seed

registry import 成功：

- basin：1
- basin version：1
- river network version：1
- mesh version：1
- model instance：1
- GIS river segments：3738

为了让本项目解析 SHUD 输出时能和 qhh `.sp.riv` 输出序号对齐，本轮额外 seed 了 qhh SHUD output river identities：

- SHUD output rivers：1633
- 标记：`properties_json.shud_output_river=true`

forcing 采用原始 qhh/rSHUD 站点表：

- source：`data/Basins/qhh/input/qhh/qhh.tsd.forc`
- station count：386
- station IDs：`qhh_forc_001` 到 `qhh_forc_386`
- forcing package 标准 SHUD 文件：`shud/qhh.tsd.forc` + 386 个 `shud/X*.csv`
- staged runtime 输入：`qhh.tsd.forc` 头部为 `386 20260521`，并复制 386 个站点 CSV 到 SHUD project 目录。
- `qhh.sp.att` 的 `FORC` 映射保留原始多站点索引；本次 staged 文件中 `FORC` 有 347 个不同取值，不再 remap 到单站点。

### GFS、canonical 与 forcing

- GFS cycle：`2026052100`
- cycle time：`2026-05-21T00:00:00Z`
- forecast hours：3-24
- raw files：56
- canonical products：56
- forcing version：`forc_gfs_2026052100_basins_qhh_shud`
- forcing package URI：`s3://nhms/forcing/gfs/2026052100/basins_qhh_vbasins/basins_qhh_shud/`
- station count：386
- timestep count：8
- forcing checksum：`86874e92f7d2dc529d85f4e42cb47fc99ec077d604c0bfab419302277df8e494`

本次复跑时 `gfs-download.stdout.json` 中 `total_bytes_written=0`，原因是 raw GRIB 文件已存在于 `.nhms-runs/qhh-smoke/raw/gfs/2026052100/`，下载阶段复用了本地缓存；状态仍为 `raw_complete`。

### SHUD runtime 与 output parse

- run ID：`qhh_gfs_2026052100_smoke`
- run status：`frequency_done`
- runtime command style：`shud_project`
- native solver：`SHUD/shud`
- output：`s3://nhms/runs/qhh_gfs_2026052100_smoke/output/`
- source file：`.nhms-runs/qhh-smoke/runs/qhh_gfs_2026052100_smoke/output/qhh.rivqdown.csv`

解析结果：

- rows written：11431
- segment count：1633
- first valid time：`2026-05-21T11:00:00+08:00`
- last valid time：`2026-05-22T05:00:00+08:00`
- min：`1.3017905092592592e-05 m3/s`
- max：`233.1804398148148 m3/s`
- avg：`12.779450083317329 m3/s`
- QC：passed
- negative values：0
- outliers：0

### API/frontend 展示数据产品

后端结果解析完成后，脚本继续执行 `scripts/publish_qhh_display_products.py`，把 qhh run 推到 API/frontend 可发现状态：

- model lifecycle：`basins_qhh_shud` 为 active。
- scenario：标准化为 `forecast_gfs_deterministic`，前端默认 GFS 查询可直接命中。
- SHUD output river geometry：1633/1633 条已从 GIS `iRiv` 分段聚合为可渲染 LineString。
- hydro layer valid times：7 个，`2026-05-21T03:00:00Z` 到 `2026-05-21T21:00:00Z`。
- `flood.return_period_result`：13064 行，其中 timestep 11431 行、peak 1633 行。
- frequency curves：0 条；所有 return-period 行标记为 `quality_flag=no_frequency_curve`，`return_period` 与 `warning_level` 不伪造。

已验证的前端消费 API：

- `GET /api/v1/basins`
- `GET /api/v1/basins/basins_qhh/versions`
- `GET /api/v1/runs?status=frequency_done&source=GFS`
- `GET /api/v1/models?basin_version_id=basins_qhh_vbasins&active=true`
- `GET /api/v1/basin-versions/basins_qhh_vbasins/river-segments?river_network_version_id=basins_qhh_rivnet_vbasins`
- `GET /api/v1/basin-versions/basins_qhh_vbasins/river-segments/{segment_id}/forecast-series`
- `GET /api/v1/layers?run_id=qhh_gfs_2026052100_smoke`
- `GET /api/v1/layers/discharge/valid-times?run_id=qhh_gfs_2026052100_smoke`
- `GET /api/v1/flood-alerts/summary?run_id=qhh_gfs_2026052100_smoke`

## 本轮发现并修复的堵点

1. 本机无 Docker 权限：新增 `scripts/local_pg.sh`，使用项目内 PostgreSQL runtime、data、socket、log 路径。
2. smoke 环境不启用 TimescaleDB：新增 `scripts/apply_smoke_migrations.py`，在本地 PG 中应用兼容迁移。
3. qhh package 缺少 lake runtime 文件：package 发布逻辑支持可选复制 `qhh.lake.sp`、`qhh.lake.bathy`、`qhh.lake.ic`。
4. qhh `seg` shapefile 有重复原始 ID：registry 几何导入对重复 raw segment ID 做最小后缀消歧。
5. GFS `f000` 部分字段不可用：smoke 默认从 forecast hour 3 开始。
6. cfgrib/ecCodes 与系统 `libstdc++` 不匹配：smoke 脚本在可用时设置 `.conda-postgres-runtime/lib/libstdc++.so.6` 为 `LD_PRELOAD`。
7. canonical grid definition 过于简化：raw/canonical 保留 lon/lat/shape，forcing producer 支持 compact rectilinear grid definition 与 0-360 经度归一化。
8. forcing producer 输出为平台内部格式，SHUD 需要 rSHUD/native 格式：runtime 阶段转换为 `qhh.tsd.forc` 与 `forcing.csv`。
9. native SHUD CLI 与 mock 不同：runtime 新增 `shud_project` command style，按 `SHUD/shud -o <output_dir> -n <threads> qhh` 运行。
10. native SHUD cfg 使用空白分隔，不是 `KEY = value`：runtime staging 修正 cfg 写法，并强制 ASCII 输出。
11. qhh 校准输入 `sp.att` 的 `FORC` 引用原始 386 站点：新增 `scripts/seed_qhh_forcing_stations.py` 从 `qhh.tsd.forc` seed 386 个站点，forcing producer 对全部站点插值并输出 SHUD 标准 `qhh.tsd.forc` + 每站 CSV，runtime staging 保留原始 `FORC` 映射。
12. registry GIS river segment 数量与 SHUD `.sp.riv` 输出数量不一致：新增 `scripts/seed_qhh_shud_output_segments.py`，parser 优先使用 SHUD output river identities。
13. SHUD `.rivqdown.csv` 含 metadata/comment/header，且 `Time_min` 是 Unix minute：runtime verifier 和 output parser 已兼容该格式并转换真实 valid time。
14. 386 站点下原 IDW 权重计算对全网格逐站排序过慢：`compute_idw_weights` 改为优先使用 `scipy.spatial.cKDTree`，无 scipy 时退回小堆近邻，保留 IDW 语义。
15. 重复复测会撞旧 qhh DB 状态：新增 `scripts/reset_qhh_smoke_db.py`，`QHH_RESET_SMOKE_DB=1` 时只清理 qhh smoke 相关 registry、forcing、run、timeseries、QC 行。
16. 前端展示发现缺 `GET /api/v1/basins` 与 `GET /api/v1/basins/{basin_id}/versions`：补齐 registry read API。
17. qhh 专用 scenario 不被前端默认 GFS 选择命中：发布步骤标准化为 `forecast_gfs_deterministic`。
18. 河段列表默认先返回 GIS rivseg 分段而非有时序结果的 SHUD 输出河段：river segment API 对 `shud_output_river=true` 的河段优先排序。

## 当前剩余工作

- 将 qhh smoke 入口纳入更正式的 orchestrator/integration lane，而不是长期依赖专用 shell。
- 进一步补与原始历史 forcing 同时段的数值复现对比；当前已恢复原始 386 站点布设和 `FORC` 映射，但气象驱动仍为 GFS `2026052100`。
- 补 qhh 可用的洪水频率曲线后，才能产出可信 `return_period` 与 `warning_level`；当前只发布 `no_frequency_curve` 质量状态。
- 在目标环境补真实 PostgreSQL/PostGIS/TimescaleDB、对象存储、Slurm 和长期 GFS cycle 稳定性证据。
- 当前复测覆盖前端展示所需 API 数据产品，但未启动浏览器做真实 UI 截图，不覆盖全国规模性能。
