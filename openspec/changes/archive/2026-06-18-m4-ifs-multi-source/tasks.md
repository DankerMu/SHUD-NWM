## 1. IFS Data Adapter (ifs-data-adapter)

- [x] 1.1 添加 `ecmwf-opendata` 依赖到 `pyproject.toml`，添加 `nhms-ifs` script 入口指向 `workers.data_adapters.cli:ifs_main`，验证安装。Evidence: `pyproject.toml`, `workers/data_adapters/cli.py`, `tests/test_ifs_adapter.py`.
- [x] 1.2 实现 `IFSAdapterConfig` 数据类：source_id/variables/cycle_hours/lead_time_policy/镜像源配置（REQ-IDA-01）。Evidence: `workers/data_adapters/ifs_adapter.py`, `tests/test_ifs_adapter.py`.
- [x] 1.3 实现 `IFSAdapter.initialize_data_source()`：注册 IFS 到 met.data_source 表（REQ-IDA-01）。Evidence: `workers/data_adapters/ifs_adapter.py`, `tests/test_ifs_adapter.py`, `tests/test_e2e_ifs.py`.
- [x] 1.4 实现 `IFSAdapter.discover_cycles()`：调用 ECMWF Open Data 检查周期可用性，upsert forecast_cycle，处理全天无数据场景（REQ-IDA-02, REQ-IDA-09）。Evidence: `workers/data_adapters/ifs_adapter.py`, `tests/test_ifs_adapter.py`.
- [x] 1.5 实现 `IFSAdapter.build_manifest()`：根据 cycle_hour 区分 168h/144h 时间步，生成 8 变量下载清单，manifest metadata 写入 max_lead_hours，持久化 manifest JSON（REQ-IDA-03）。Evidence: `workers/data_adapters/ifs_adapter.py`, `tests/test_ifs_adapter.py`, `tests/test_e2e_ifs.py`.
- [x] 1.6 实现 `IFSAdapter.download_plan()`：通过 ecmwf-opendata 客户端下载，支持镜像切换、轮询等待、重试退避、幂等检查、HTTP 429 限流处理（REQ-IDA-04, REQ-IDA-08）。Evidence: `workers/data_adapters/ifs_adapter.py`, `tests/test_ifs_adapter.py`.
- [x] 1.7 实现 `IFSAdapter.verify_manifest()`：文件存在性、大小、SHA256 校验，空 GRIB 文件检测，使用 status=`"passed"` 与基类一致（REQ-IDA-05）。Evidence: `workers/data_adapters/ifs_adapter.py`, `tests/test_ifs_adapter.py`.
- [x] 1.8 实现 IFS CLI 入口 `ifs_main()` 和下载命令到 `workers/data_adapters/cli.py`（REQ-IDA-07）。Evidence: `workers/data_adapters/cli.py`, `pyproject.toml`, `tests/test_ifs_adapter.py`.
- [x] 1.9 编写 IFS adapter 单元测试：mock ECMWF 客户端，覆盖 discover/manifest/download/verify 全流程 + 06/18 周期 144h + 幂等 + 失败重试 + 429 限流 + 空 GRIB + 全天无数据。Evidence: `tests/test_ifs_adapter.py`.

## 2. IFS Canonical 转换 (ifs-data-adapter)

- [x] 2.1 新增 `IFS_VARIABLE_MAPPING` 字典：tp/2t/2d/10u/10v/sp/ssr/str → 标准变量，气压映射到 `surface_pressure`（REQ-IDA-06）。Evidence: `workers/canonical_converter/converter.py`, `tests/test_ifs_canonical.py`.
- [x] 2.2 实现 `IFSCanonicalConverter`：继承 CanonicalConverter，处理 IFS 特有转换逻辑：RH 从 T+Td 计算、tp m→mm/step、辐射 ssr+str 累积差分→W/m²（REQ-IDA-06）。Evidence: `workers/canonical_converter/converter.py`, `tests/test_ifs_canonical.py`.
- [x] 2.3 更新 `canonical_converter/cli.py`：添加 IFS source_id 分支，实例化 IFSCanonicalConverter（REQ-IDA-07）。Evidence: `workers/canonical_converter/cli.py`, `tests/test_ifs_canonical.py`.
- [x] 2.4 编写 canonical 转换单元测试：覆盖温度/RH/降水/辐射/风/气压、负降水处理、lineage_json 验证。Evidence: `tests/test_ifs_canonical.py`.

## 3. IFS 预报闭环集成 (ifs-forecast-integration)

- [x] 3.1 将 scenario mapping 迁移到 `services/orchestrator/chain.py` 生产代码，`OrchestratorConfig.source_id` 自动派生 scenario_id（REQ-IFI-02）。Evidence: `services/orchestrator/chain.py`, `tests/test_ifs_forecast_integration.py`.
- [x] 3.2 修改编排器 `_build_run_manifest()` 和 `_build_run_context()`：run_id 前缀使用 `fcst_{source}_...`，scenario_id 使用动态值，end_time 根据 max_lead_hours 动态计算（REQ-IFI-03, REQ-IFI-04）。Evidence: `services/orchestrator/chain.py`, `tests/test_ifs_forecast_integration.py`, `tests/test_e2e_ifs.py`.
- [x] 3.3 修改 forcing producer：接受 `source_id` 参数，支持 IFS `net_radiation`、降水 mm/step→mm/day、`forc_{source}_...` ID、144h 限制和 lineage_json max_lead_hours（REQ-IFI-01）。Evidence: `workers/forcing_producer/producer.py`, `tests/test_ifs_forecast_integration.py`, `tests/test_e2e_ifs.py`.
- [x] 3.4 更新 Slurm sbatch 模板：接受 `source_id` 参数，IFS 调用 `nhms-ifs`，GFS 保持现有行为（REQ-IFI-06）。Evidence: `infra/sbatch/download_source_cycle.sbatch`, `tests/test_slurm_array_contract.py`.
- [x] 3.5 实现 IFS 自动触发逻辑：canonical_ready → 启动 forcing+forecast 链，支持 IFS 延迟到达后独立触发（REQ-IFI-05）。Evidence: `services/orchestrator/chain.py`, `tests/test_ifs_forecast_integration.py`.
- [x] 3.6 编写编排器多源集成测试：模拟 GFS+IFS 并行运行，验证互不干扰、scenario_id 正确、run_id 唯一、06/18 end_time=144h。Evidence: `tests/test_ifs_forecast_integration.py`, `tests/test_e2e_ifs.py`.

## 4. API 多源增强 (multi-source-comparison-ui)

- [x] 4.1 更新 forecast_store 查询：per-source latest 策略，response series 元素增加 `source_id`、`cycle_time`、`available_lead_hours` 字段（REQ-UI-07）。Evidence: `packages/common/forecast_store.py`, `apps/api/routes/forecast.py`, `tests/test_forecast_api.py`, `tests/test_e2e_ifs.py`.
- [x] 4.2 更新 OpenAPI spec `nhms.v1.yaml`：RiverSeriesResponse.series 添加 source_id/cycle_time/available_lead_hours 字段（REQ-UI-07）。Evidence: `openapi/nhms.v1.yaml`, `apps/frontend/src/api/types.ts`.
- [x] 4.3 编写 API 多源查询测试：GFS+IFS 双源返回、per-source latest、06/18 available_lead_hours=144、include_analysis 不重复。Evidence: `tests/test_forecast_api.py`, `tests/test_e2e_ifs.py`.

## 5. 前端多源对比 UI (multi-source-comparison-ui)

- [x] 5.1 修改 `stores/forecast.ts`：新增 `selectedScenarios` 状态（默认 `["GFS"]`），`fetchForecastSeries` 传递动态 scenarios 参数（REQ-UI-05）。Evidence: `apps/frontend/src/stores/forecast.ts`, `apps/frontend/src/__tests__/ForecastComparison.test.tsx`.
- [x] 5.2 实现 scenario 选择器组件：复选框组 `GFS`/`IFS`，至少保留一个选中，绑定 store 状态（REQ-UI-01）。Evidence: `apps/frontend/src/components/ScenarioSelector.tsx`, `apps/frontend/src/__tests__/ForecastComparison.test.tsx`.
- [x] 5.3 修改 `ForecastChart.tsx`：IFS 曲线使用绿色虚线 `#2ca02c`，tooltip 显示多 scenario 值，图例增强（REQ-UI-02）。Evidence: `apps/frontend/src/components/charts/ForecastChart.tsx`, `apps/frontend/src/__tests__/ForecastComparison.test.tsx`.
- [x] 5.4 实现 06/18 周期 6d 标注：根据 series.available_lead_hours=144 在 IFS 曲线末端添加虚线垂直标注 + "IFS 6d" 标签（REQ-UI-03）。Evidence: `apps/frontend/src/components/charts/ForecastChart.tsx`, `apps/frontend/src/__tests__/ForecastComparison.test.tsx`.
- [x] 5.5 增强起报信息面板：显示数据源列表和各源起报时刻（从 series.cycle_time 读取）（REQ-UI-04）。Evidence: `apps/frontend/src/components/forecast/ForecastPanel.tsx`, `apps/frontend/src/stores/forecast.ts`, `apps/frontend/src/__tests__/ForecastComparison.test.tsx`.
- [x] 5.6 实现 IFS 数据不可用降级：复选框旁灰色提示 `(暂无数据)`（REQ-UI-06）。Evidence: `apps/frontend/src/components/ScenarioSelector.tsx`, `apps/frontend/src/__tests__/ForecastComparison.test.tsx`.
- [x] 5.7 编写前端组件测试：覆盖 scenario 切换、双曲线渲染、6d 标注、降级显示。Evidence: `apps/frontend/src/__tests__/ForecastComparison.test.tsx`.

## 6. 端到端验证

- [x] 6.1 添加 IFS demo seed 数据到 `db/seeds/seed_demo.py`：IFS data_source 记录 + IFS forecast_cycle + hydro_run + river_timeseries 样本。Evidence: `db/seeds/seed_demo.py`, `tests/test_seed.py`.
- [x] 6.2 编写 IFS E2E 测试 `tests/test_e2e_ifs.py`：mock ECMWF 文件，覆盖 adapter→canonical→forcing→run→parse→API→UI contract，含 06Z 144h 场景。Evidence: `tests/test_e2e_ifs.py`.
