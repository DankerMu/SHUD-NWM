## ADDED Requirements

### Requirement: Hindcast 提交 API

系统 SHALL 提供 hindcast 提交接口，允许 operator/model_admin 提交历史回放任务。

#### Scenario: 提交单流域 30 年 hindcast

- **WHEN** 调用 `POST /api/v1/hindcast/submit` 传入 `{"model_id": "yangtze_shud_v12", "source_id": "ERA5", "start_time": "1993-01-01T00:00:00Z", "end_time": "2023-12-31T23:00:00Z", "purpose": "flood_frequency_sample"}`
- **THEN** 系统从时间范围派生日历年列表（1993-2023，共 31 年），为每年创建一个 `hydro_run` 记录，run_type = `"hindcast"`，scenario_id = `"hindcast_replay"`
- **AND** 每个 run 的 run_id 格式为 `hindcast_era5_{model_id}_{year}`（如 `hindcast_era5_yangtze_shud_v12_1993`）
- **AND** 返回响应包含 `total_runs`、`run_ids` 列表、`slurm_job_array_id`

#### Scenario: 部分年份已完成

- **WHEN** 提交 hindcast 且部分年份的 run 已存在且 status = `"succeeded"`
- **THEN** 跳过已成功的年份，仅为未完成年份创建新 run
- **AND** 返回 `skipped_years` 列表

#### Scenario: 权限不足

- **WHEN** viewer 或 analyst 角色调用 hindcast/submit
- **THEN** 返回 403 Forbidden，error_code = `"PERMISSION_DENIED"`

---

### Requirement: Hindcast 年切片编排

系统 SHALL 将 hindcast 请求拆分为按水文年的独立 Slurm 作业。

#### Scenario: Slurm job array 提交

- **WHEN** hindcast 提交包含 N 个年切片
- **THEN** 使用 Slurm job array `--array=0-{N-1}` 一次提交
- **AND** 每个 array task 的工作流为：ERA5 forcing 生产 → SHUD hindcast 运行 → output 解析 → river_timeseries 入库
- **AND** pipeline_job 表记录每个切片的 slurm_job_id 和 array_task_id

#### Scenario: 单年切片失败

- **WHEN** 某一年的 hindcast 运行失败（如 ERA5 数据缺失）
- **THEN** 该年 hydro_run status = `"failed"`，error_code 和 error_message 记录原因
- **AND** 其他年份不受影响，继续运行
- **AND** 失败年份可通过 `POST /api/v1/runs/{run_id}/retry` 单独重试

#### Scenario: Hindcast sbatch 模板

- **WHEN** Slurm 提交 hindcast 作业
- **THEN** 使用 `hindcast.sbatch` 模板，接受参数：`model_id`、`source_id`、`year`、`workspace_root`
- **AND** 模板调用 `nhms-flood hindcast-year --model-id ... --source-id ERA5 --year ...`

---

### Requirement: Hindcast Forcing 生产

系统 SHALL 复用现有 forcing 生产流程为 hindcast 年切片生成 forcing 数据。

#### Scenario: 单年 ERA5 forcing 生产

- **WHEN** hindcast 年切片开始执行
- **THEN** 从 ERA5 canonical 产品中提取该年 1 月 1 日至 12 月 31 日的气象数据
- **AND** 调用 forcing producer 为 model_id 对应的所有 met_station 生成 forcing
- **AND** forcing_version_id 格式为 `forc_era5_hindcast_{model_id}_{year}`
- **AND** forcing_version.lineage_json 记录 `{"purpose": "hindcast", "year": YYYY}`

#### Scenario: ERA5 canonical 数据不完整

- **WHEN** 某年的 ERA5 canonical 数据覆盖不足（变量缺失或时间轴不连续）
- **THEN** QC 标记 quality_flag = `"incomplete_forcing"`
- **AND** 若缺失率 > 10%，该年 hindcast 失败并记录 error_code = `"INSUFFICIENT_ERA5_COVERAGE"`

---

### Requirement: Hindcast 数据隔离

Hindcast 结果 SHALL 入库到同一 `hydro.river_timeseries` 表但遵循隔离规则。

#### Scenario: Hindcast 入库

- **WHEN** hindcast 年切片的 output parser 完成
- **THEN** river_timeseries 行的 run_id 指向 hindcast run
- **AND** 数据可被频率引擎通过 `JOIN hydro.hydro_run hr ON rt.run_id = hr.run_id WHERE hr.run_type = 'hindcast'` 查询（run_type 在 hydro_run 表，非 river_timeseries）

#### Scenario: Hindcast 不产生 StateSnapshot

- **WHEN** hindcast 运行完成
- **THEN** 不在 `hydro.state_snapshot` 表中创建记录
- **AND** 不影响业务 warm-start 链路（forecast/analysis 的 init_state_id 查询不返回 hindcast 产物）

#### Scenario: 前端默认不展示 hindcast

- **WHEN** 前端查询 forecast-series API
- **THEN** 默认不返回 hindcast scenario 的数据
- **AND** analyst 角色可通过 `?run_types=hindcast` 参数查询 hindcast 结果

---

### Requirement: Hindcast CLI 入口

系统 SHALL 提供 CLI 命令用于 hindcast 操作。

#### Scenario: 提交 hindcast

- **WHEN** 执行 `nhms-flood hindcast-submit --model-id yangtze_shud_v12 --start-time 1993-01-01 --end-time 2023-12-31`
- **THEN** 调用 hindcast 提交 API，输出 run_ids 和 slurm_job_array_id

#### Scenario: 单年执行

- **WHEN** 在 Slurm 作业中执行 `nhms-flood hindcast-year --model-id yangtze_shud_v12 --source-id ERA5 --year 2005`
- **THEN** 完成该年的 forcing 生产 → SHUD 运行 → output 解析 → 入库全流程
- **AND** 成功时 hydro_run status 从 `"running"` 转为 `"parsed"`

#### Scenario: 查询 hindcast 进度

- **WHEN** 执行 `nhms-flood hindcast-status --model-id yangtze_shud_v12`
- **THEN** 输出各年切片的 run_id、status、耗时
