## Why

M1 实现了 GFS forecast 冷启动闭环，但 cold-start 预报缺乏真实初始状态，精度受限。M2 引入 Analysis Run 用真实场/再分析 forcing 持续更新 SHUD 状态，并让 Forecast Run 从最新 StateSnapshot warm-start，同时前端需要拼接过去 7 天 analysis 曲线与未来 7 天 forecast 曲线，形成完整的时序展示。

## What Changes

- 新增 ERA5 adapter：支持 CDS API cycle discovery、GRIB 下载、canonical 转换（变量映射、累计量差分、J/m² → W/m² 等）
- 新增 Analysis Run pipeline：ERA5 forcing → SHUD analysis → StateSnapshot 生成与管理
- 新增 State Manager：StateSnapshot 存储（`.cfg.ic`）、查询最近可用状态、usable_flag 管理、状态过旧标记 degraded
- 修改 Forecast pipeline：支持 init_state_id → init_state_uri → SHUD INIT_MODE=3 warm-start
- 新增 best_available_selection 表写入与查询逻辑
- 新增前端曲线拼接：analysis_true_field（过去 7 天）+ forecast（未来 7 天），含资料来源和分界线标注

## Capabilities

### New Capabilities

- `era5-data-acquisition`: ERA5 CDS API adapter，cycle discovery、GRIB 下载、canonical 转换（含 ERA5 特有的累计量差分、露点→RH 计算、J/m² 辐射→W/m²）
- `analysis-run-pipeline`: 真实场 analysis run 编排，ERA5 forcing → SHUD analysis → 河段结果入库 + StateSnapshot 生成
- `state-manager`: StateSnapshot 生命周期管理——存储 `.cfg.ic`、查询最近可用状态、usable_flag 切换、状态过旧检测与 degraded 标记
- `forecast-warm-start`: Forecast run 从 StateSnapshot warm-start，选择最近可用 state、init_state_uri 注入 manifest、SHUD INIT_MODE=3 启动
- `best-available-selection`: best_available 产品规则——按时间窗口和数据源优先级写入 met.best_available_selection，支持来源追溯
- `analysis-forecast-curve-splicing`: 前端曲线拼接——analysis_true_field 过去 7 天 + forecast 未来 7 天，含分界线、资料来源标注、scenario 切换

### Modified Capabilities

（无已有 spec 需要修改）

## Impact

- **数据库**：`hydro.state_snapshot`（新增写入）、`hydro.hydro_run`（新增 run_type=analysis、init_state_id 填充）、`met.best_available_selection`（新增写入）、`hydro.river_timeseries`（新增 analysis 结果）
- **API**：新增 `GET /api/v1/state-snapshots`、修改 forecast-series 接口支持 analysis + forecast 拼接
- **HPC/Slurm**：新增 ERA5 download job、analysis run job、state snapshot 存储 job
- **前端**：预报曲线页面改造，支持双段拼接和分界线
- **对象存储**：新增 `raw/ERA5/`、`canonical/ERA5/`、`states/{model_id}/{valid_time}/` 路径
- **依赖**：新增 cdsapi（ERA5 CDS API 客户端）
