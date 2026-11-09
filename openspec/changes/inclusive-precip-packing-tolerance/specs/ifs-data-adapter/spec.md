## MODIFIED Requirements

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
THEN 执行累积差分得到步长降水量，单位从 m 转为 mm（× 1000），再按实际步长换算为 mm/day（× 24 / step_hours）
AND 负差分处理按 canonical-conversion「IFS precipitation de-accumulation tolerance follows GRIB quantisation」：噪声界与比较方向（含步长已知时的闭区间边界，#3109）以该 requirement 为准；小于等于噪声界的负差分 → 0（quality_flag=ok）；超出噪声界 → 0（quality_flag=warning_negative_precip）；≥3 连续负值 → error_precip_accumulation
AND `lineage_json` 记录 `{"accumulation_type": "since_cycle", "unit_conversion": "m_to_mm_day", "step_hours": 3}`

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
