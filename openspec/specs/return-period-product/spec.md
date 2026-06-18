# return-period-product Specification

## Purpose
TBD - created by archiving change m5-flood-frequency-warning. Update Purpose after archive.
## Requirements
### Requirement: Forecast 重现期自动计算

每次 forecast run 解析完成后，系统 SHALL 自动计算该 run 中所有河段的重现期。

#### Scenario: 正常计算

- **WHEN** forecast run 的 output parser 完成（hydro_run status = `"parsed"`）
- **THEN** 频率阶段自动触发
- **AND** 对该 run 中每个 river_segment，提取未来 7 天（或可用时效内）最大预报流量 Q_max
- **AND** 查询 `flood.flood_frequency_curve`（匹配 model_id + river_network_version_id + river_segment_id + duration=`"1h"` + quality_flag IN ('ok','partial_sample','monotonicity_corrected')，取最新 sample_period_end）获取 Q2-Q100 阈值
- **AND** 通过对数线性插值计算 return_period T
- **AND** 映射 warning_level
- **AND** 写入 `flood.return_period_result`

#### Scenario: 频率曲线不存在

- **WHEN** 某河段没有 quality_flag=`"ok"` 的频率曲线
- **THEN** 该河段的 return_period = `null`，warning_level = `null`
- **AND** quality_flag = `"no_frequency_curve"`
- **AND** 不阻塞其他河段的计算

#### Scenario: 状态机转换

- **WHEN** 所有河段的重现期计算完成
- **THEN** hydro_run status 从 `"parsed"` 转为 `"frequency_done"`
- **AND** pipeline_job 表记录 frequency 阶段的 start_time、end_time、status

---

### Requirement: Max-over-window 提取

系统 SHALL 从预报时间序列中提取指定时间窗口内的最大流量。

#### Scenario: 7 天窗口

- **WHEN** 对 forecast run 提取 max_over_window
- **THEN** 取 `river_timeseries` 中 valid_time 在 [run.start_time, run.end_time] 范围内该河段的瞬时 `MAX(value)` WHERE variable=`"q_down"`（与 duration=`"1h"` 频率曲线语义匹配）
- **AND** `return_period_result.max_over_window` = `true`
- **AND** `return_period_result.valid_time` 设为取得最大值的时刻

#### Scenario: 06/18 IFS 6 天窗口

- **WHEN** IFS 06/18 周期 forecast run 的 end_time 仅覆盖 6 天
- **THEN** max_over_window 仅在实际可用时段内提取
- **AND** `return_period_result.duration` 反映实际窗口

---

### Requirement: 对数线性插值计算重现期

系统 SHALL 使用对数线性插值从频率曲线反推重现期。

#### Scenario: Q 落在两个阈值之间

- **WHEN** Q_max = 2600 m³/s，频率曲线 Q10=2300, Q20=2900
- **THEN** 在 (log10(10), 2300) 和 (log10(20), 2900) 之间线性插值
- **AND** return_period ≈ 15.0（log T vs Q 线性）

#### Scenario: Q 低于 Q2

- **WHEN** Q_max = 800 m³/s < Q2=1200
- **THEN** return_period = 1.0（或 < 2）
- **AND** warning_level = `"normal"`

#### Scenario: Q 超过 Q100

- **WHEN** Q_max = 5000 m³/s > Q100=4500
- **THEN** return_period > 100（不做高外推点估计）
- **AND** warning_level = `"extreme"`

---

### Requirement: Warning Level 映射

系统 SHALL 按固定阈值映射重现期到 7 级预警等级。

#### Scenario: 7 级映射规则

- **WHEN** return_period 已计算
- **THEN** 按以下规则映射 warning_level：
  - T < 2 → `"normal"`
  - 2 ≤ T < 5 → `"elevated"`
  - 5 ≤ T < 10 → `"watch"`
  - 10 ≤ T < 20 → `"warning"`
  - 20 ≤ T < 50 → `"high_risk"`
  - 50 ≤ T < 100 → `"severe"`
  - T ≥ 100 → `"extreme"`

#### Scenario: 频率曲线某级 sample 不足

- **WHEN** 频率曲线的 `parameters_json.sample_quality.Q50.quality_flag` = `"insufficient_sample"`
- **THEN** Q50 阈值不参与正式预警判定
- **AND** 若 Q_max 落入 Q20-Q50 区间但 Q50 不可靠，warning_level 降级为 `"warning"`（取可靠的最高等级）
- **AND** return_period_result.quality_flag = `"unreliable_threshold"`

#### Scenario: 频率曲线 quality_flag = fit_failed 或 no_valid_sample

- **WHEN** 频率曲线的整体 quality_flag = `"fit_failed"` 或 `"no_valid_sample"`
- **THEN** 等同于频率曲线不存在，return_period = `null`，warning_level = `null`
- **AND** quality_flag = `"no_usable_frequency_curve"`

---

### Requirement: 重现期结果入库

计算结果 SHALL 写入 `flood.return_period_result` 表。

#### Scenario: 正常入库

- **WHEN** 重现期计算完成
- **THEN** 写入字段：run_id, scenario_id, basin_version_id, river_network_version_id, model_id, river_segment_id, valid_time, duration, q_value, q_unit=`"m3/s"`, return_period, warning_level, source_id, cycle_time, max_over_window, quality_flag
- **AND** 主键 `(run_id, river_segment_id, duration, valid_time)` 保证唯一

#### Scenario: 逐时刻结果（必需）

- **WHEN** forecast run 的重现期计算执行
- **THEN** 除 max_over_window 行外，还 SHALL 为每个预报时刻计算重现期
- **AND** 每个时刻一行 `return_period_result`，`max_over_window` = `false`
- **AND** API 和 UI 的时间步切换、timeline 依赖此数据

---

### Requirement: Slurm 依赖链集成

频率计算 SHALL 作为 Slurm 依赖链的 frequency 阶段执行。

#### Scenario: parse 后自动触发

- **WHEN** parse 阶段 Slurm 作业成功完成
- **THEN** frequency 阶段通过 `--dependency=afterok:$PARSE_JOB_ID` 自动启动
- **AND** 使用 `frequency.sbatch` 模板，调用 `nhms-flood compute-return-period --run-id ...`

#### Scenario: frequency 失败不阻塞发布

- **WHEN** frequency 阶段失败（如频率曲线不存在）
- **THEN** hydro_run status 保持 `"parsed"`（不转为 `"frequency_done"`）
- **AND** 可通过 `POST /api/v1/runs/{run_id}/retry` 重试 frequency 阶段
- **AND** publish 阶段可配置为允许跳过 frequency（graceful degradation）

---

### Requirement: 重现期 CLI

系统 SHALL 提供 CLI 命令用于重现期计算。

#### Scenario: 按 run_id 计算

- **WHEN** 执行 `nhms-flood compute-return-period --run-id fcst_gfs_2026050300_yangtze_v12`
- **THEN** 计算该 run 所有河段的重现期并入库
- **AND** 输出统计：total_segments, with_curve, without_curve, warning_counts_by_level

