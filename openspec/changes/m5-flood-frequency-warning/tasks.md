## 1. Hindcast 回放能力 (hindcast-replay)

- [x] 1.1 添加 `scipy` 依赖到 `pyproject.toml`，添加 `nhms-flood` script 入口指向 `workers.flood_frequency.cli:main`，验证安装。Evidence: `pyproject.toml`, `workers/flood_frequency/cli.py`, `tests/test_flood_frequency.py`.
- [x] 1.2 创建 `workers/flood_frequency/` 目录结构：`__init__.py`、`cli.py`、`hindcast.py`、`frequency.py`、`return_period.py`、`config.py`。Evidence: `workers/flood_frequency/`.
- [x] 1.3 实现 hindcast 提交逻辑：接收 model_id/source_id/start_time/end_time，服务端派生日历年列表，按年切片生成 hydro_run 记录，跳过已成功年份，返回 run_ids 列表。Evidence: `workers/flood_frequency/hindcast.py`, `tests/test_hindcast.py`.
- [x] 1.4 实现 hindcast 年切片 forcing 生产：从 ERA5 canonical 提取整年数据，生成 forcing_version，lineage_json 记录 purpose/year，ERA5 覆盖不足时失败。Evidence: `workers/flood_frequency/hindcast.py`, `tests/test_hindcast.py`.
- [x] 1.5 实现 hindcast 年切片执行逻辑 `hindcast_year()`：forcing → SHUD hindcast → output parser → river_timeseries，不产生 StateSnapshot。Evidence: `workers/flood_frequency/hindcast.py`, `tests/test_hindcast.py`.
- [x] 1.6 创建 `hindcast.sbatch` 模板：接受 model_id/source_id/year/workspace_root 参数，调用 `nhms-flood hindcast-year`。Evidence: `infra/sbatch/hindcast.sbatch`, `tests/test_slurm_array_contract.py`.
- [x] 1.7 实现 hindcast Slurm 编排：job array 并行提交，pipeline_job 表记录 slurm_job_id 和 array_task_id。Evidence: `workers/flood_frequency/hindcast.py`, `db/migrations/000012_pipeline_job_array_task.sql`, `tests/test_hindcast.py`.
- [x] 1.8 实现 hindcast 单年失败重试：通过 `POST /api/v1/runs/{run_id}/retry` 重试单年切片，恢复 pipeline_job 和 hydro_run 状态。Evidence: `services/orchestrator/retry.py`, `tests/test_hindcast.py`, `tests/test_retry.py`.
- [x] 1.9 实现 hindcast API 端点 `POST /api/v1/hindcast/submit`：权限检查、输入校验、调用提交逻辑、返回统计。Evidence: `apps/api/routes/hindcast.py`, `tests/test_hindcast.py`.
- [x] 1.10 实现 hindcast 数据隔离：forecast-series/runs 默认排除 hindcast，analyst 可显式查询。Evidence: `apps/api/routes/forecast.py`, `packages/common/forecast_store.py`, `tests/test_hindcast.py`.
- [x] 1.11 实现 hindcast CLI 入口：`hindcast-submit`、`hindcast-year`、`hindcast-status`。Evidence: `workers/flood_frequency/cli.py`, `tests/test_hindcast.py`.
- [x] 1.12 编写 hindcast 单元/集成测试：覆盖年切片、幂等、单年执行、forcing 不完整失败、失败重试、权限、数据隔离、输入校验。Evidence: `tests/test_hindcast.py`.

## 2. 洪水频率曲线拟合 (flood-frequency-fitting)

- [x] 2.1 实现年最大值样本提取 `extract_annual_maxima()`：按 6 种 duration 从 river_timeseries 提取，滑动窗口平均，排除缺测年份，0 样本返回空列表。Evidence: `workers/flood_frequency/frequency.py`, `tests/test_flood_frequency.py`.
- [x] 2.2 实现 P-III 拟合 `fit_pearson3()`：调用 `scipy.stats.pearson3.fit()`，计算 Q2-Q100。Evidence: `workers/flood_frequency/frequency.py`, `tests/test_flood_frequency.py`.
- [x] 2.3 实现 GEV 拟合 `fit_gev()`：调用 `scipy.stats.genextreme.fit()`，计算 Q2-Q100。Evidence: `workers/flood_frequency/frequency.py`, `tests/test_flood_frequency.py`.
- [x] 2.4 实现拟合策略 `fit_frequency_curve()`：默认 P-III，失败 fallback GEV；双失败写 fit_failed/QC。Evidence: `workers/flood_frequency/frequency.py`, `tests/test_flood_frequency.py`.
- [x] 2.5 实现样本量检查 `check_sample_size()`：按等级检查最小年数，生成 per-threshold sample_quality。Evidence: `workers/flood_frequency/frequency.py`, `tests/test_flood_frequency.py`.
- [x] 2.6 实现单调性校验与修正 `check_monotonicity()`。Evidence: `workers/flood_frequency/frequency.py`, `tests/test_flood_frequency.py`.
- [x] 2.7 实现频率曲线入库 `save_frequency_curve()`：UPSERT 幂等，curve_id 包含唯一维度，0 样本时写占位记录。Evidence: `workers/flood_frequency/frequency.py`, `tests/test_flood_frequency.py`.
- [x] 2.8 实现 QC 结果写入。Evidence: `workers/flood_frequency/frequency.py`, `tests/test_flood_frequency.py`.
- [x] 2.9 实现模型版本更新处理：旧曲线 quality_flag 改为 superseded_by_model_upgrade。Evidence: `workers/flood_frequency/frequency.py`, `tests/test_flood_frequency.py`.
- [x] 2.10 实现频率引擎 CLI：`fit-curves --model-id ...`，支持全模型/单河段/dry-run。Evidence: `workers/flood_frequency/cli.py`, `tests/test_flood_frequency.py`.
- [x] 2.11 编写频率拟合单元测试。Evidence: `tests/test_flood_frequency.py`.

## 3. 重现期产品 (return-period-product)

- [x] 3.1 实现 max_over_window 提取 `extract_max_forecast_q()`。Evidence: `workers/flood_frequency/return_period.py`, `tests/test_return_period.py`.
- [x] 3.2 实现逐时刻提取 `extract_timestep_q()`。Evidence: `workers/flood_frequency/return_period.py`, `tests/test_return_period.py`.
- [x] 3.3 实现频率曲线查询 `get_frequency_curve()`。Evidence: `workers/flood_frequency/return_period.py`, `tests/test_return_period.py`.
- [x] 3.4 实现对数线性插值 `interpolate_return_period()`。Evidence: `workers/flood_frequency/return_period.py`, `tests/test_return_period.py`.
- [x] 3.5 实现 warning_level 映射 `map_warning_level()`，按 sample_quality 降级，不可用曲线返回 null。Evidence: `workers/flood_frequency/return_period.py`, `tests/test_return_period.py`.
- [x] 3.6 实现重现期批量计算 `compute_return_periods()`：max_over_window + 逐时刻计算，UPSERT 入库。Evidence: `workers/flood_frequency/return_period.py`, `tests/test_return_period.py`.
- [x] 3.7 实现 hydro_run 状态机转换：成功 parsed → frequency_done；失败记录 error。Evidence: `workers/flood_frequency/return_period.py`, `tests/test_return_period.py`.
- [x] 3.8 实现 frequency 失败处理：失败不阻塞发布（graceful degradation），支持 retry。Evidence: `workers/flood_frequency/return_period.py`, `services/orchestrator/retry.py`, `tests/test_return_period.py`.
- [x] 3.9 创建 frequency sbatch 模板：parse 后触发，调用 `nhms-flood compute-return-period --run-id` 或 array manifest。Evidence: `infra/sbatch/compute_frequency_array.sbatch`, `tests/test_slurm_array_contract.py`.
- [x] 3.10 实现重现期 CLI：`compute-return-period --run-id`。Evidence: `workers/flood_frequency/cli.py`, `tests/test_return_period.py`.
- [x] 3.11 编写重现期计算单元测试。Evidence: `tests/test_return_period.py`.

## 4. 预警聚合 API (flood-alert-api)

- [x] 4.1 创建 `apps/api/routes/flood_alerts.py`：注册 flood alert 端点。Evidence: `apps/api/routes/flood_alerts.py`, `apps/api/main.py`.
- [x] 4.2 实现 `GET /api/v1/flood-alerts/summary`。Evidence: `apps/api/routes/flood_alerts.py`, `tests/test_flood_alerts_api.py`.
- [x] 4.3 实现 `GET /api/v1/flood-alerts/ranking`。Evidence: `apps/api/routes/flood_alerts.py`, `tests/test_flood_alerts_api.py`.
- [x] 4.4 实现 `GET /api/v1/flood-alerts/segments`。Evidence: `apps/api/routes/flood_alerts.py`, `tests/test_flood_alerts_api.py`.
- [x] 4.5 实现 `GET /api/v1/flood-alerts/timeline`。Evidence: `apps/api/routes/flood_alerts.py`, `tests/test_flood_alerts_api.py`.
- [x] 4.6 修改 forecast-series API：响应中嵌入 `frequency_thresholds`。Evidence: `packages/common/forecast_store.py`, `apps/api/routes/forecast.py`, `tests/test_flood_alerts_api.py`.
- [x] 4.7 实现瓦片端点 `GET /api/v1/tiles/flood-return-period/...pbf`，返回 flood return-period features。Evidence: `apps/api/routes/flood_alerts.py`, `tests/test_flood_alerts_api.py`.
- [x] 4.8 注册 hindcast API `POST /api/v1/hindcast/submit` 到 router。Evidence: `apps/api/routes/hindcast.py`, `apps/api/main.py`, `tests/test_hindcast.py`.
- [x] 4.9 更新 OpenAPI spec `nhms.v1.yaml`：添加 flood-alerts、tile、hindcast、frequency_thresholds/sample_quality schema。Evidence: `openapi/nhms.v1.yaml`, `apps/frontend/src/api/types.ts`.
- [x] 4.10 编写 API 测试。Evidence: `tests/test_flood_alerts_api.py`, `tests/test_hindcast.py`.

## 5. 前端预警地图页 (flood-warning-map-ui)

- [x] 5.1 创建 `stores/floodAlert.ts`。Evidence: `apps/frontend/src/stores/floodAlert.ts`.
- [x] 5.2 在 router 中添加“洪水预警” Tab 路由。Evidence: `apps/frontend/src/App.tsx`, `apps/frontend/src/components/layout/NavBar.tsx`.
- [x] 5.3 实现 `FloodAlertPage.tsx` 页面骨架。Evidence: `apps/frontend/src/pages/FloodAlertPage.tsx`.
- [x] 5.4 实现左侧预警统计面板组件 `AlertStatsPanel`。Evidence: `apps/frontend/src/components/flood/AlertStatsPanel.tsx`, `apps/frontend/src/components/flood/__tests__/FloodAlertComponents.test.tsx`.
- [x] 5.5 实现右侧 TOP 排名面板组件 `AlertRankingPanel`。Evidence: `apps/frontend/src/components/flood/AlertRankingPanel.tsx`, `apps/frontend/src/components/flood/__tests__/FloodAlertComponents.test.tsx`.
- [x] 5.6 实现地图预警瓦片图层加载。Evidence: `apps/frontend/src/components/flood/FloodReturnPeriodLayer.tsx`, `apps/frontend/src/components/flood/FloodAlertMap.tsx`.
- [x] 5.7 实现地图交互：hover tooltip + click 触发详情加载。Evidence: `apps/frontend/src/components/flood/FloodAlertMap.tsx`.
- [x] 5.8 实现顶部预警信息滚动条 `AlertTicker`。Evidence: `apps/frontend/src/components/flood/AlertTicker.tsx`.
- [x] 5.9 实现底部时间轴基础组件。Evidence: `apps/frontend/src/components/flood/AlertTimeline.tsx`.
- [x] 5.10 实现时间步切换同步。Evidence: `apps/frontend/src/pages/FloodAlertPage.tsx`, `apps/frontend/src/stores/floodAlert.ts`.
- [x] 5.11 实现播放模式。Evidence: `apps/frontend/src/pages/FloodAlertPage.tsx`, `apps/frontend/src/components/flood/AlertTimeline.tsx`.
- [x] 5.12 实现河段详情面板数据加载 `SegmentAlertDetail`。Evidence: `apps/frontend/src/components/flood/SegmentAlertDetail.tsx`.
- [x] 5.13 实现河段详情面板渲染：预报曲线 + Q2-Q100 参考线 + 预警时间线。Evidence: `apps/frontend/src/components/flood/SegmentAlertDetail.tsx`.
- [x] 5.14 实现预警矢量瓦片生成逻辑：frequency_done 后注册 `map.tile_layer`，layer_type=flood_return_period。Evidence: `workers/flood_frequency/return_period.py`, `tests/test_return_period.py`.
- [x] 5.15 编写前端组件测试。Evidence: `apps/frontend/src/components/flood/__tests__/FloodAlertComponents.test.tsx`.

## 6. 端到端验证

- [x] 6.1 添加 flood demo seed 数据到 `db/seeds/`：flood_frequency_curve、return_period_result、hindcast run 记录。Evidence: `db/seeds/seed_demo.py`, `tests/test_seed.py`.
- [x] 6.2 编写 E2E/集成测试覆盖 hindcast → 年切片 → 频率拟合 → forecast run → 重现期计算 → API 查询 → 前端渲染链路。Evidence: `tests/test_hindcast.py`, `tests/test_flood_frequency.py`, `tests/test_return_period.py`, `tests/test_flood_alerts_api.py`, `apps/frontend/src/components/flood/__tests__/FloodAlertComponents.test.tsx`.
- [x] 6.3 验收检查：频率曲线与 model_id+river_network_version_id 强绑定、Q2<Q5<...<Q100、样本不足 per-threshold 标记、forecast 后自动计算重现期、逐时刻数据可用、前端地图着色正确、时间步切换同步。Evidence: `tests/test_flood_frequency.py`, `tests/test_return_period.py`, `tests/test_flood_alerts_api.py`, `apps/frontend/src/components/flood/alertLevels.ts`.
