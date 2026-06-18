# ifs-forecast-integration Specification

## Purpose
TBD - created by archiving change m4-ifs-multi-source. Update Purpose after archive.
## Requirements
### Requirement: IFS Forcing 生产

系统 SHALL 满足「IFS Forcing 生产」要求。

IFS canonical 数据可通过现有 forcing 生产流程转换为 SHUD 模型驱动数据。

#### Scenario: 正常 forcing 生产

WHEN IFS forecast_cycle status = `"canonical_ready"`
AND 调用 forcing 生产流程，source_id = `"IFS"`
THEN 对每个 model_instance 的每个 met_station 执行空间插值
AND 生成 forcing_version 记录，`source_id` = `"IFS"`
AND forcing_version_id 格式 = `"forc_ifs_{compact_cycle}_{model_id}"`
AND forecast_cycle status 更新为 `"forcing_ready"`

#### Scenario: IFS RH 使用计算值

WHEN forcing 生产读取 canonical `relative_humidity_2m`
THEN 使用 IFS canonical 转换阶段已计算好的 RH 值（来自 Magnus 公式）
AND 不再二次计算

#### Scenario: 06/18 周期 forcing 时间范围

WHEN 源为 IFS 06/18 周期（max_lead_hours = 144）
THEN forcing 时间范围覆盖 0h-144h（非 0h-168h）
AND forcing_version.lineage_json 记录 `"max_lead_hours": 144`

#### Scenario: IFS net_radiation 直接使用

WHEN source_id=IFS 且 canonical 产品包含 `net_radiation` 但不包含 `shortwave_down`
THEN forcing producer 直接使用 `net_radiation` 作为 SHUD Rn 输入
AND 不因缺少 `shortwave_down` 报错
AND forcing_version_component 关联 Rn 到 net_radiation canonical 产品

#### Scenario: IFS 降水 mm/step 转 mm/day

WHEN forcing producer 读取 IFS canonical 降水（单位 mm/step, step_hours=3）
THEN 转为 mm/day：`PRCP_mm_day = prcp_mm_per_step × (24 / step_hours)`
AND 转换仅在 forcing 阶段执行一次

---

### Requirement: 编排器多源 Scenario 路由

系统 SHALL 满足「编排器多源 Scenario 路由」要求。

编排器根据 source_id 动态确定 scenario_id，支持 GFS 和 IFS 独立编排。

#### Scenario: _scenario_for_source() 生产代码

WHEN 编排器初始化
THEN 使用生产代码中的 `_scenario_for_source(source_id)` 函数（从 tests/ 迁移到 services/orchestrator/chain.py）
AND 映射：GFS → forecast_gfs_deterministic，IFS → forecast_ifs_deterministic

#### Scenario: GFS 编排链（保持现有行为）

WHEN 编排器以 `source_id="GFS"` 启动
THEN `scenario_id` = `"forecast_gfs_deterministic"`
AND 完整执行 forcing → forecast → parse 链
AND hydro_run 和 river_timeseries 写入结果与 M3 阶段一致

#### Scenario: IFS 编排链

WHEN 编排器以 `source_id="IFS"` 启动
THEN `scenario_id` = `"forecast_ifs_deterministic"`
AND 完整执行 forcing → forecast → parse 链
AND hydro_run 记录 `source_id="IFS"`, `scenario_id="forecast_ifs_deterministic"`

#### Scenario: GFS 和 IFS 独立运行

WHEN 同一 cycle_time 的 GFS 和 IFS 编排链同时运行
THEN 两条链互不阻塞、互不影响
AND 各自独立写入 hydro_run 和 river_timeseries

#### Scenario: IFS 编排链失败不影响 GFS

WHEN IFS 编排链在任意阶段失败
THEN GFS 编排链不受影响，继续正常运行
AND IFS hydro_run status 更新为 `"failed"`，记录 error_code 和 error_message

---

### Requirement: IFS Hydro Run 记录

系统 SHALL 满足「IFS Hydro Run 记录」要求。

IFS 预报运行的 hydro_run 记录准确反映数据来源和场景。

#### Scenario: IFS hydro_run 字段

WHEN IFS 预报运行创建 hydro_run 记录
THEN 字段值包括：
  - `run_id` = `"fcst_ifs_{compact_cycle}_{model_id}"`
  - `run_type` = `"forecast"`
  - `scenario_id` = `"forecast_ifs_deterministic"`
  - `source_id` = `"IFS"`
  - `cycle_time` = IFS 起报时刻

#### Scenario: Run ID 唯一性

WHEN 同一 cycle_time 同时存在 GFS 和 IFS 预报
THEN run_id 前缀区分：`fcst_gfs_...` vs `fcst_ifs_...`
AND 不会主键冲突

---

### Requirement: IFS 结果解析入库

系统 SHALL 满足「IFS 结果解析入库」要求。

IFS 预报运行的 SHUD 输出解析后写入 river_timeseries。

#### Scenario: 正常解析入库

WHEN IFS hydro_run status = `"succeeded"`
THEN output_parser 读取 SHUD 输出文件
AND 写入 `hydro.river_timeseries`，关联 IFS 的 run_id
AND hydro_run status 更新为 `"parsed"`

#### Scenario: 06/18 周期 end_time 动态设置

WHEN 编排器为 IFS 06/18 周期构建 run context
THEN hydro_run.end_time = cycle_time + 144h（而非固定 168h）
AND run_manifest 中 forecast_horizon_hours = 144

#### Scenario: 06/18 周期结果时间范围

WHEN 解析 IFS 06/18 周期的预报结果
THEN river_timeseries 时间范围为 start_time 到 start_time + 144h（共 49 个时间步）
AND 不包含 144h-168h 的数据点

---

### Requirement: IFS 自动触发

系统 SHALL 满足「IFS 自动触发」要求。

IFS 周期完成数据准备后自动触发预报链。

#### Scenario: 自动触发条件

WHEN IFS forecast_cycle status 变为 `"canonical_ready"`
AND 对应模型实例可用
THEN 编排器自动启动 IFS forcing → forecast → parse 链

#### Scenario: 周期已处理（幂等）

WHEN 编排器检测到该 IFS cycle_time 已有 status ∈ {succeeded, parsed, published} 的 hydro_run
THEN 跳过该周期，不重复运行

#### Scenario: IFS 延迟到达后自动触发

WHEN GFS 00Z 已 published，IFS 00Z 数小时后变为 canonical_ready
THEN GFS 保持 published 状态不变
AND IFS 自动启动 forcing → forecast → parse 链
AND 后续 multi-source API 查询可包含 IFS series

---

### Requirement: Slurm 模板多源支持

系统 SHALL 满足「Slurm 模板多源支持」要求。

Slurm sbatch 模板支持 IFS 数据源。

#### Scenario: sbatch 模板路由

WHEN Slurm 提交 IFS 下载任务
THEN sbatch 模板调用 `nhms-ifs`（而非硬编码 `nhms-gfs`）
AND 模板接受 `source_id` 和 `cycle_time` 参数动态路由

#### Scenario: sbatch 模板向后兼容

WHEN source_id 未指定或为 GFS
THEN 行为与 M3 阶段一致，调用 `nhms-gfs`

