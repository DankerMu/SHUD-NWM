## Why

系统已完成 GFS+IFS 双源预报闭环和 Slurm 全国化调度（M0-M4），但河段预报曲线只显示流量值，无法回答"这个流量有多严重"。洪水频率分析是将预报流量转化为重现期和预警等级的核心能力，是系统从"模拟平台"升级为"预警产品"的关键一步。前置条件（ERA5 analysis run、多流域并行）已全部就绪。

## What Changes

- **新增 Hindcast 回放能力**：通过 ERA5 历史气象数据驱动 SHUD 模型回放 30+ 年，生成每条河段的历史流量序列，作为频率分析的样本来源。提供 hindcast 提交 API 和 Slurm 作业集成。
- **新增洪水频率引擎**：从 `hydro.river_timeseries` 中按 6 种 duration（1h/3h/6h/24h/72h/7d）提取年最大值序列，用 P-III（默认）或 GEV 方法拟合频率曲线，计算 Q2/Q5/Q10/Q20/Q50/Q100 阈值，入库 `flood.flood_frequency_curve`。包含样本量检查、单调性校验、quality_flag 标记。
- **新增重现期产品**：每次 forecast run 完成后自动提取未来 7 天最大预报流量，查频率曲线计算重现期，映射为 7 级 warning_level（normal → extreme），入库 `flood.return_period_result`。hydro_run 状态机新增 `frequency_done` 转换。
- **新增预警聚合 API**：提供 summary（各级河段数量统计）、ranking（按重现期降序排名）、segments（按条件筛选）、timeline（单河段预警时间线）四个端点。
- **新增前端预警地图页**：河段按 7 级预警等级着色（灰/蓝/黄/橙/红/深红/紫），左侧预警统计面板，右侧 TOP 排名面板，支持时间步切换和播放，点击河段跳转至详情面板。
- **新增预警矢量瓦片**：发布 flood-return-period vector tiles，支持地图缩放和快速渲染。

## Capabilities

### New Capabilities
- `hindcast-replay`: Hindcast 历史回放能力——hindcast 提交 API、ERA5 多年连续 forcing 生产、Slurm hindcast 作业编排、river_timeseries 历史样本入库、hindcast 数据隔离规则
- `flood-frequency-fitting`: 洪水频率曲线拟合——年最大值/POT 样本提取（6 种 duration）、P-III/GEV 拟合、Q2-Q100 阈值计算、样本量检查、单调性校验与修正、quality_flag 标记、flood_frequency_curve 入库、模型版本更新时曲线重算、CLI 入口
- `return-period-product`: 实时重现期产品——forecast run 后自动计算、未来 7 天 max_over_window 提取、频率曲线查询与对数线性插值、warning_level 映射、return_period_result 入库、hydro_run 状态机 frequency_done 阶段集成
- `flood-alert-api`: 预警聚合 API——summary/ranking/segments/timeline 四个端点、按 run_id 查询、warning_level 过滤、分页排序
- `flood-warning-map-ui`: 前端预警地图页——河段 7 级着色（重现期图层）、预警统计面板、TOP 排名面板、时间步切换与播放、河段点击详情联动、预警矢量瓦片发布（flood-return-period vector tiles）

### Modified Capabilities

## Impact

- **新增文件**：`workers/flood_frequency/` 目录（频率引擎、重现期计算、CLI）、`apps/api/routers/flood_alerts.py`、`apps/api/routers/hindcast.py`、`apps/frontend/src/pages/FloodAlertPage.tsx`、`apps/frontend/src/components/flood/` 组件目录
- **修改文件**：`services/orchestrator/chain.py`（hindcast 编排 + frequency_done 阶段）、`workers/sbatch_templates/`（hindcast + frequency sbatch）、`apps/api/routers/forecast.py`（frequency_thresholds 嵌入曲线响应）、`apps/frontend/src/router.ts`（洪水预警 Tab）、`apps/frontend/src/stores/`（FloodAlertState）
- **数据库**：无新 migration——`flood.flood_frequency_curve` 和 `flood.return_period_result` 表已在 M0 migration `000007_flood.sql` 中创建；hydro_run 状态机已包含 `frequency_done` 状态
- **API**：新增 4 个 flood-alerts 端点 + 1 个 hindcast/submit 端点；现有 forecast-series 响应中嵌入 `frequency_thresholds` 对象
- **依赖**：新增 `scipy`（P-III/GEV 拟合）；前端无新依赖（复用 MapLibre + ECharts）
- **Slurm**：新增 `hindcast.sbatch` 和 `frequency.sbatch` 模板
