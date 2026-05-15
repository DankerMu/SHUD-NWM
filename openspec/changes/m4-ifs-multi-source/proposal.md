## Why

系统当前仅支持 GFS 单一气象数据源驱动预报。IFS（ECMWF Open Data）是全球精度最高的确定性预报模式之一，接入 IFS 后可实现同一河段 GFS+IFS 双曲线对比，提升预报可信度和决策支持能力。阶段 5（M1-M2）已打通预报闭环，阶段 6 的 Slurm 调度已完成，IFS 接入的前置条件全部就绪。

## What Changes

- **新增 IFS 数据适配器**：对接 ECMWF Open Data API（ecmwf-opendata Python 客户端），支持 00/06/12/18 四个周期的自动发现、GRIB2 下载、校验，并转换为系统标准 canonical 格式。
- **IFS 变量映射与转换**：IFS 使用不同的参数编码（`tp`, `2t`, `2d`, `10u`, `10v`, `sp`, `ssr`, `str`），需新增 IFS 专用变量映射和单位转换规则（如 `tp` 单位为 m→mm，RH 需从 T+Td 计算）。
- **IFS 预报闭环集成**：复用现有 forcing 生产和 SHUD 运行流程，IFS 预报结果以 `scenario_id=forecast_ifs_deterministic` 独立存储。
- **编排器多源支持**：编排器 `OrchestratorConfig.scenario_id` 当前硬编码为 `forecast_gfs_deterministic`，需支持按 `source_id` 动态路由。
- **前端多源对比 UI**：scenario 选择器（复选框组）、GFS 橙色/IFS 绿色虚线双曲线、IFS 06/18 周期 6 天可用时效标注。
- **IFS 06/18 周期不足 7 天处理**：06/18 UTC 最大预报时效仅 144h（6 天），需在 metadata 和前端明确标注 `available_lead_range`。

## Capabilities

### New Capabilities
- `ifs-data-adapter`: IFS ECMWF Open Data 适配器，包括周期发现、GRIB2 下载与校验、canonical 格式转换、IFS 变量映射（tp/2t/2d/10u/10v/sp/ssr/str → 标准变量）
- `ifs-forecast-integration`: IFS 数据驱动的预报闭环集成，包括 forcing 生产复用、编排器多源 scenario 路由、forecast_cycle 状态管理、CLI 入口
- `multi-source-comparison-ui`: 前端多源对比能力，包括 scenario 选择器控件、GFS/IFS 双曲线渲染（颜色/线型区分）、06/18 周期可用时效标注、图例增强

### Modified Capabilities

## Impact

- **新增文件**：`workers/data_adapters/ifs_adapter.py`、`workers/canonical_converter/` 中 IFS 转换配置
- **修改文件**：`workers/canonical_converter/cli.py`（IFS 分支）、`services/orchestrator/chain.py`（scenario 路由）、`apps/frontend/src/stores/forecast.ts`（scenarios 参数）、`apps/frontend/src/components/charts/ForecastChart.tsx`（多线渲染）、`apps/frontend/src/components/forecast/ForecastPanel.tsx`（选择器 UI）
- **依赖**：新增 `ecmwf-opendata` Python 包
- **数据库**：无 migration，现有 `met.data_source` + `hydro_run.scenario_id` 已支持
- **API**：无新端点，`?scenarios=GFS,IFS` 后端已实现
