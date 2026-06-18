## ADDED Requirements

### Requirement: IFS 数据源注册

系统 SHALL 满足「IFS 数据源注册」要求。

系统启动时自动在 `met.data_source` 表中注册 IFS 数据源。

#### Scenario: IFS 数据源初始化

WHEN IFS adapter 首次初始化
THEN `met.data_source` 表中存在记录：
  - `source_id` = `"IFS"`
  - `source_name` = `"IFS Open Data"`
  - `source_type` = `"forecast"`
  - `status` = `"enabled"`
  - `native_format` = `"GRIB2"`
  - `adapter_name` = `"ifs_adapter"`
  - `config_json` 包含 `cycle_hours_utc`, `lead_time_policy`, `variables`, `preferred_source`, `fallback_sources`

#### Scenario: 重复初始化幂等

WHEN IFS adapter 再次初始化且 `met.data_source` 已有 IFS 记录
THEN 更新 `config_json` 但不报错

---

### Requirement: IFS 周期发现

系统 SHALL 满足「IFS 周期发现」要求。

适配器能自动发现指定日期的可用 IFS 预报周期。

#### Scenario: 发现单日全部 4 个周期

WHEN 调用 `discover_cycles(cycle_date="2026-05-01")`
THEN 返回最多 4 个 `CycleDiscovery`，cycle_hour 分别为 0, 6, 12, 18
AND 每个 CycleDiscovery 包含 `available: bool` 标记
AND 可用周期在 `met.forecast_cycle` 中 upsert 记录，status = `"discovered"`

#### Scenario: 发现日期范围内的周期

WHEN 调用 `discover_cycles(cycle_date="2026-05-01", end_date="2026-05-03")`
THEN 返回 3 天 × 4 周期 = 最多 12 个 CycleDiscovery

#### Scenario: 数据尚未发布

WHEN 目标周期的 IFS 数据尚未在 ECMWF Open Data 上发布
THEN 对应 CycleDiscovery 的 `available` = `false`
AND 不在 `met.forecast_cycle` 中创建记录

---

### Requirement: IFS 下载清单构建

系统 SHALL 满足「IFS 下载清单构建」要求。

为已发现的周期构建 GRIB2 文件下载清单。

#### Scenario: 00/12 周期清单（168h）

WHEN 调用 `build_manifest(cycle_time="2026-05-01T00:00:00Z")`
THEN 清单包含 forecast_hour 0, 3, 6, ..., 168（共 57 个时间步）
AND 每个时间步包含 8 个变量：`2t`, `2d`, `10u`, `10v`, `tp`, `sp`, `ssr`, `str`
AND `manifest.source_id` = `"IFS"`
AND manifest JSON 持久化到 `raw/IFS/{compact_cycle}/manifest.json`

#### Scenario: 06/18 周期清单（144h）

WHEN 调用 `build_manifest(cycle_time="2026-05-01T06:00:00Z")`
THEN 清单包含 forecast_hour 0, 3, 6, ..., 144（共 49 个时间步）
AND manifest metadata 包含 `"max_lead_hours": 144`

#### Scenario: 自定义 forecast_hours

WHEN 调用 `build_manifest(cycle_time=..., forecast_hours=[0, 3, 6])`
THEN 清单仅包含指定的 3 个时间步

---

### Requirement: IFS GRIB2 下载

系统 SHALL 满足「IFS GRIB2 下载」要求。

从 ECMWF Open Data 下载 GRIB2 文件到本地对象存储。

#### Scenario: 正常下载全部文件

WHEN 调用 `download_plan(manifest)` 且所有文件可用
THEN 所有 GRIB2 文件写入 `raw/IFS/{compact_cycle}/` 目录
AND 每个文件计算 SHA256 校验和
AND `met.forecast_cycle` status 更新为 `"raw_complete"`

#### Scenario: 镜像源切换

WHEN 主源（ecmwf）下载失败
THEN 自动切换到 fallback 源（aws → azure → google）
AND 切换行为记录到日志

#### Scenario: 文件暂时不可用（轮询等待）

WHEN 部分 forecast_hour 文件返回 404
THEN 以配置的 `poll_interval_seconds`（默认 600s）轮询
AND 最大等待 `max_wait_seconds`（默认 14400s / 4h）
AND 超时后标记 forecast_cycle status = `"failed_download"` 并记录 error_code

#### Scenario: 幂等下载

WHEN 调用 `download_plan` 且所有文件已存在且校验和匹配
THEN 返回 `status="already_done"`，不重复下载

#### Scenario: 下载重试

WHEN 单个文件下载失败（网络错误）
THEN 最多重试 `max_retries`（默认 3 次），指数退避（1s, 2s, 4s）
AND 全部重试失败后标记该文件为 failed

---

### Requirement: IFS 下载文件校验

系统 SHALL 满足「IFS 下载文件校验」要求。

对已下载的 GRIB2 文件进行完整性校验。

#### Scenario: 校验通过

WHEN 调用 `verify_manifest(manifest)` 且所有文件存在、大小合理、校验和匹配
THEN 返回 `VerificationResult(status="passed")`

#### Scenario: 文件缺失或损坏

WHEN 某文件不存在或校验和不匹配
THEN 返回 `VerificationResult(status="failed", failures=[...])`
AND failures 列出具体文件和失败原因

#### Scenario: 空 GRIB 文件检测

WHEN verify_manifest 发现某 GRIB2 文件 size=0 或无有效 GRIB message
THEN 返回 `VerificationResult(status="failed", failures=[...])`
AND failures 包含 error_code=`"EMPTY_FILE"` 或 `"INVALID_GRIB"`
AND forecast_cycle 不标记为 `"raw_complete"`

---

### Requirement: ECMWF 限流处理

系统 SHALL 满足「ECMWF 限流处理」要求。

适配器正确处理 ECMWF Open Data API 的速率限制。

#### Scenario: HTTP 429 响应

WHEN ECMWF 返回 HTTP 429 且 Retry-After=120
THEN adapter 等待至少 120s（或配置的上限等待时间取较小值）
AND 记录 error_code=`"RATE_LIMITED"`
AND 不立即标记 cycle 为 failed，继续重试/切换镜像源

#### Scenario: 持续限流

WHEN 所有镜像源均持续返回 429 超过 max_wait_seconds
THEN 标记 forecast_cycle status = `"failed_download"`
AND error_message 包含 "rate limited" 和尝试过的源列表

---

### Requirement: 全天无数据处理

系统 SHALL 满足「全天无数据处理」要求。

#### Scenario: 当日所有 4 个周期均不可用

WHEN discover_cycles 对 2026-05-01 发现 00/06/12/18 均不可用
THEN 返回 4 个 CycleDiscovery，available 均为 false
AND 不创建任何 forecast_cycle 或 hydro_run 记录
AND 适配器正常退出，不抛异常

---

### Requirement: IFS Canonical 格式转换

系统 SHALL 满足「IFS Canonical 格式转换」要求。

将 IFS GRIB2 原始数据转换为系统标准 canonical 格式。

#### Scenario: 温度转换（2t → air_temperature_2m）

WHEN 处理 IFS `2t` 变量
THEN 输出 canonical 变量 `air_temperature_2m`，单位 `degC`（K − 273.15）
AND `lineage_json` 记录 `{"unit_conversion": "K_to_C"}`

#### Scenario: 相对湿度计算（2t + 2d → relative_humidity_2m）

WHEN 处理 IFS `2t`（温度）和 `2d`（露点温度）
THEN 先将 K 转为 °C（T = 2t - 273.15, Td = 2d - 273.15）
AND 使用 Magnus 公式计算 RH：`RH = exp((17.625 × Td) / (243.04 + Td)) / exp((17.625 × T) / (243.04 + T))`
AND 输出 canonical 变量 `relative_humidity_2m`，单位 `0-1`，值域 [0, 1]
AND `lineage_json` 记录 `{"derived_from": ["2t", "2d"], "method": "magnus_formula"}`

#### Scenario: 相对湿度数值验证

WHEN IFS 2t=293.15K（20°C），2d=283.15K（10°C）
THEN RH = exp(17.625×10/253.04) / exp(17.625×20/263.04) ≈ 0.525（容差 1e-3）

#### Scenario: 降水转换（tp → prcp_rate_or_amount）

WHEN 处理 IFS `tp`（总降水，累积，单位 m）
THEN 执行累积差分得到步长降水量（mm/step）
AND 单位从 m 转为 mm（× 1000），不在 canonical 阶段转 mm/day（mm/day 由 forcing 阶段处理）
AND 负差分处理：|Δ| < 0.01 mm → 0（quality_flag=ok）；|Δ| ≥ 0.01 mm → 0（quality_flag=warning_negative_precip）；≥3 连续负值 → error_precip_accumulation
AND `lineage_json` 记录 `{"accumulation_type": "since_cycle", "unit_conversion": "m_to_mm", "step_hours": 3}`

#### Scenario: 降水转换数值验证

WHEN IFS tp 在 f003=0.003m，f006=0.006m，step_hours=3
THEN f006 的 canonical 降水 = (0.006 - 0.003) × 1000 = 3.0 mm/step
AND unit = `"mm"`，quality_flag = `"ok"`

#### Scenario: 辐射转换（ssr + str → net_radiation）

WHEN 处理 IFS `ssr`（净短波辐射）和 `str`（净长波辐射）
THEN 累积 J/m² 差分得到步长能量
AND 转为 W/m²：`Rn = (ssr_step + str_step) / (step_hours × 3600)`
AND `lineage_json` 记录 `{"radiation_method": "direct_net", "components": ["ssr", "str"]}`

#### Scenario: 风速转换（10u + 10v）

WHEN 处理 IFS `10u` 和 `10v`
THEN 输出 `wind_u_10m` 和 `wind_v_10m`，单位 m/s，直接映射
AND forcing 生产阶段合成风速：`wind = sqrt(u² + v²)`

#### Scenario: 气压转换（sp → surface_pressure）

WHEN 处理 IFS `sp`
THEN 输出 `surface_pressure`，单位 Pa，直接映射

#### Scenario: Canonical 产品入库

WHEN 所有变量转换完成
THEN 每个 (variable, forecast_hour) 组合在 `met.canonical_met_product` 中创建记录
AND `source_id` = `"IFS"`，`grid_id` = `"ifs_0p25"`
AND `met.forecast_cycle` status 更新为 `"canonical_ready"`

---

### Requirement: IFS CLI 入口

系统 SHALL 满足「IFS CLI 入口」要求。

提供命令行入口执行 IFS 数据下载和转换。

#### Scenario: CLI 下载命令

WHEN 执行 `nhms-ifs download --cycle-time 2026050100`
THEN 依次执行 discover → manifest → download → verify
AND 成功后 forecast_cycle status = `"raw_complete"`

#### Scenario: CLI 转换命令

WHEN 执行 `nhms-canonical convert --source-id IFS --cycle-time 2026050100`
THEN 加载 IFS manifest，执行 canonical 转换
AND 成功后 forecast_cycle status = `"canonical_ready"`
