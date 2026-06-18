## Why

M0 交付了项目骨架、数据库、契约和 Mock 环境，但系统尚无真实数据流入和预报产出能力。M1 需要打通从 GFS 气象数据获取到前端河段预报曲线展示的完整闭环，证明架构端到端可行，为后续多源（IFS/ERA5/CLDAS）、多流域、Analysis warm-start 等能力奠定基础。

## What Changes

- 新增 GFS adapter：实现 cycle discovery、raw download、manifest 生成
- 新增 Canonical converter：GRIB2 → 标准变量/单位/时间轴，写入 `met.canonical_met_product`
- 新增 Forcing producer：格点→代站插值，生成 `.tsd.forc` + forcing_station_timeseries
- 新增 Model registry 操作：注册 demo model_instance（basin_version + river_network_version + SHUD 版本）
- 新增 SHUD runtime adapter：workspace 准备 + `shud_omp` 执行 + 输出上传
- 新增 Output parser：`.rivqdown` 解析，m³/d → m³/s 转换，`hydro.river_timeseries` 入库
- 新增 Slurm 作业依赖链：download → canonical → forcing → forecast → parse 五阶段编排
- 新增 API 端点：河段预报曲线查询 `forecast-series`、基础 run 查询
- 新增前端页面：河网底图 + 河段点击弹窗 + 预报曲线图

## Capabilities

### New Capabilities

- `gfs-data-acquisition`: GFS 资料源适配——cycle discovery（00/06/12/18 UTC）、GRIB2 下载、manifest 生成、多通道 fallback、幂等重跑
- `canonical-conversion`: 统一气象中间产品转换——变量标准化（7 个标准变量）、单位转换（K→℃、累计→时段量）、时间轴生成、lineage_json 血缘记录
- `forcing-production`: SHUD forcing 生产——气象代站定义、格点→代站插值权重、6 个 forcing 变量生成（PRCP/TEMP/RH/wind/Rn/Press）、`.tsd.forc` + CSV 输出
- `model-registration`: 模型资产注册——demo basin_version + river_network_version + mesh_version + model_instance 注册、模型包完整性校验、activate API
- `shud-runtime`: SHUD 运行适配——workspace 拉取（模型包+forcing+配置）、`.cfg.para` 生成、`shud_omp` 执行、输出完整性校验、结果上传对象存储
- `output-parsing`: SHUD 输出解析入库——`.rivqdown` 解析（m³/d→m³/s）、列数/河段数一致性校验、`hydro.river_timeseries` 写入、QC 检查
- `slurm-job-chain`: Slurm 作业依赖链编排——5 阶段 sbatch 模板、lazy submission（逐 stage 提交）、pipeline_job/pipeline_event 记录、Mock Gateway 集成
- `forecast-api`: 预报查询 API——河段预报曲线（`forecast-series`）、run 查询、scenario 支持预留
- `forecast-frontend`: 前端预报展示——MapLibre 河网底图、河段 hover/click 交互、预报曲线图（ECharts/类似）、时间轴

### Modified Capabilities

（无——M1 所有功能均为新建，M0 的 mock-slurm-gateway 以 `backend=mock` 模式被复用但不修改其 spec）

## Impact

- **数据库表**：写入 `met.data_source`、`met.forecast_cycle`、`met.canonical_met_product`、`met.met_station`、`met.interp_weight`、`met.forcing_version`、`met.forcing_version_component`、`met.forcing_station_timeseries`、`core.model_instance`、`hydro.hydro_run`、`hydro.river_timeseries`、`ops.pipeline_job`、`ops.pipeline_event`、`ops.qc_result`
- **对象存储**：使用 `raw/`、`canonical/`、`forcing/`、`models/`、`runs/` 五类 prefix
- **外部依赖**：GFS 数据源（NOAA/NCEP）、cfgrib/xarray（GRIB2 解析）、scipy（插值）、MapLibre GL JS
- **API 新增**：`/api/v1/data-sources`、`/api/v1/basin-versions/{basin_version_id}/river-segments/{segment_id}/forecast-series`、`/api/v1/runs`、`/api/v1/met/stations`
- **前端新增**：全国河网 GIS 页面（`apps/web/`）
