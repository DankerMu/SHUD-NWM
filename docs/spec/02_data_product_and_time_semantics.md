# 02. 数据产品与时间语义

版本：v0.2  
日期：2026-05-06

## 1. 目标

全国水文模拟系统同时处理再分析资料、近实时实况资料、数值天气预报、SHUD 模型输出和前端可视化产品。不同资料源的发布时间、有效时间、预报时效、可用延迟、原生时间分辨率不同，因此必须统一时间语义。

## 2. 标准时间字段

| 字段 | 定义 | 示例 |
|---|---|---|
| `cycle_time` | 资料起报或发布周期时间。 | `2026-04-30T00:00:00Z` |
| `issue_time` | 系统认为该资料可用于业务的发布时间。 | `2026-04-30T04:20:00Z` |
| `valid_time` | 数据代表的真实有效时间。 | `2026-05-01T06:00:00Z` |
| `lead_time_hours` | `valid_time - cycle_time`。再分析可为 0 或 null。 | `30` |
| `ingest_time` | 系统下载或登记时间。 | `2026-04-30T04:31:12Z` |
| `publish_time` | 产品对前端可见时间。 | `2026-04-30T06:10:00Z` |
| `native_time_resolution` | 数据源原生时间分辨率。 | `PT3H`、`PT1H` |
| `model_output_interval` | SHUD 输出间隔。 | `PT1H`、`PT3H` |

## 3. Data source 状态

```text
enabled      已可生产
restricted   代码支持但权限未开通，例如 CLDAS 初期
planned      计划接入
mock         测试源
deprecated   停用但历史可查
```

## 4. 资料源配置模板

```yaml
source: GFS
status: enabled
provider: NOAA/NCEP
native_format: GRIB2
cycle_hours_utc: [0, 6, 12, 18]
variables:
  temperature_2m: tmp2m
  precipitation: apcp
  relative_humidity: rh2m
  wind_u_10m: u10m
  wind_v_10m: v10m
  pressure_surface: pressfc
  shortwave_down: dswrf
latency_rule: "poll until all required forecast hours exist"
license_note: "public/open data; verify operational terms before production"
```

## 5. Canonical Meteorological Product

```text
canonical_product_id
source
source_version
cycle_time
valid_time
lead_time_hours
variable
unit
grid_id
grid_definition_uri
native_time_resolution
native_spatial_resolution
object_uri
checksum
quality_flag
lineage_json
```

## 6. 标准变量

| 标准变量 | SHUD forcing 对应 | 推荐单位 | 备注 |
|---|---|---|---|
| `prcp_rate_or_amount` | `PRCP` | `mm/day` | 累计降水在 canonical converter 内差分并按真实步长换算为日率；forcing 生产原样透传。 |
| `air_temperature_2m` | `TEMP` | `degC` | K 转 ℃。 |
| `relative_humidity_2m` | `RH` | `0-1` | 百分数转 0–1。 |
| `wind_speed_10m` | `wind` | `m/s` | 可由 U/V 分量合成。 |
| `net_radiation` | `Rn` | `W/m2` | 可由短波/长波及地表参数估算。 |
| `surface_pressure` | `Press` | `Pa` | 可缺省，但生产建议尽量提供。 |

## 7. Forcing 变量转换规则

### 7.1 降水 PRCP

系统内部 canonical 标准为 `prcp_rate_or_amount`，单位固定为 `mm/day`。forcing 输出 `PRCP` 也是 `mm/day`，生产器只做空间插值和格式化，不再从 `mm/step` 二次换算。

降水转换的核心原则：**raw 选择器先明确累计口径，canonical converter 再用真实相邻时效步长换算日率**。各 data_source adapter 必须在 source policy / lineage 中声明累计语义，避免 forcing producer 依赖静态 `native_time_resolution` 重新推导。

Canonical metadata 中降水相关字段：

```json
{
  "accumulation_type": "since_cycle | interval | instant",
  "accumulation_start": "2026-04-30T00:00:00Z",
  "accumulation_end": "2026-04-30T03:00:00Z",
  "step_hours": 3
}
```

GFS APCP 转换流程：

```text
1. GFS adapter 对 FV3-GFS APCP 双记录执行 selector policy：优先选择 stepRange = 0-fhr 的 cumulative_since_cycle 记录；f000 只保留点值变量，APCP/DSWRF 这类区间变量没有 f000 entry。
2. 转换器对相邻 forecast hour 的 0-fhr 累计值做差分，得到本时段降水量。
3. 负差分处理：
   a. |负值| < 0.01 mm → 置零，quality_flag = 'ok'
   b. |负值| ≥ 0.01 mm → 置零，quality_flag = 'warn' / source-specific warning
   c. 连续或显著异常由 source-specific converter 标记，forcing 只消费 usable canonical 产品
4. 转换器立即换算并持久化 canonical `mm/day`：
   prcp_mm_day = delta_mm × (24 / step_hours)
5. forcing producer 要求 canonical 降水 unit == `mm/day`。对 GFS，SHUD forcing 行时间表示区间起点：`cycle+0h` 行使用 f000 点值变量和 f003 区间变量，`cycle+3h` 行使用 f003 点值变量和 f006 区间变量，依此类推。
```

GFS forcing package 的 `start_time/end_time` 表示覆盖窗口，不等同于最后一行时间。168h 预报应写成 `row_time_range = 0..165h`、`time_range/end_time = 168h`；最后一行 `165h` 覆盖 `165-168h`。

ERA5 转换流程：ERA5 降水变量由 converter 按产品累计语义差分并换算为 `mm/day`；是否需要差分由 adapter/source policy 的 `accumulation_type` 决定，避免不同 ERA5 产品（ERA5、ERA5-Land、小时聚合）之间语义混淆。

CLDAS 转换流程：CLDAS adapter 标记 `accumulation_type = 'instant'`（瞬时降水率 mm/h），由 converter 标准化为 `mm/day` 后进入 canonical。

### 7.2 净辐射 Rn

不同数据源对 Rn 的支持程度不同，按以下优先级降级：

```text
Level 1：数据源直接提供 net_radiation
  适用：ERA5 提供 ssr + str（短波净 + 长波净）
  Rn = ssr + str
  注意：若 ssr/str 为累计能量（J/m²），必须除以累计时长秒数转为 W/m²；
        若已为平均通量（W/m²），则不再除时长。由 adapter metadata 声明单位。
  lineage_json.radiation_method = 'direct_net'

Level 2：用下行辐射分量推算
  适用：GFS 提供 dswrf（短波下行）+ dlwrf（长波下行）
  Rn = dswrf × (1 - albedo) + dlwrf - σ × T⁴
  albedo 取模型 mesh 属性或默认 0.23
  lineage_json.radiation_method = 'downward_components'
  quality_flag = 'estimated_radiation'

Level 3：仅有短波辐射，用经验公式近似
  Rn = dswrf × 0.77 - σ × T⁴ × (0.34 - 0.14 × √e_a)
  lineage_json.radiation_method = 'empirical_fao56'
  quality_flag = 'empirical_radiation'
```

所有 forcing 输出必须在 `lineage_json` 中记录 `radiation_method`。

### 7.3 相对湿度 RH

```text
情况 1：数据源提供 relative_humidity（%）
  RH = rh_percent / 100，范围 [0, 1]

情况 2：数据源提供 specific_humidity q（kg/kg）
  需要 air_temperature 和 surface_pressure
  e_s = 6.112 × exp(17.67 × T / (T + 243.5))   (hPa, T in ℃)
  e   = q × P_hPa / (0.622 + 0.378 × q)         (实际水汽压 hPa)
  RH  = clamp(e / e_s, 0, 1)

  如果 surface_pressure 缺失：
    使用站点高程的标准大气近似：P = 1013.25 × (1 - 2.25577e-5 × elev)^5.25588
    quality_flag = 'estimated_pressure'

情况 3：湿度变量完全缺失
  quality_flag = 'error_missing_humidity'
  该站该变量阻断，forcing_version 不得进入 ready 状态
```

## 8. Scenario 语义

| Scenario | 含义 |
|---|---|
| `analysis_true_field` | 真实场/再分析驱动的 analysis 结果。 |
| `forecast_gfs_deterministic` | GFS 确定性预报。 |
| `forecast_ifs_deterministic` | IFS 确定性预报。 |
| `forecast_best_available` | 按优先级拼接或融合后的业务产品。 |
| `forecast_gfs_ifs_compare` | 前端对比展示，不一定需要实体派生表。 |
| `hindcast_replay` | 历史回放或复盘。 |

## 9. Best available 产品规则

`best_available` 不应覆盖 GFS/IFS 原始 scenario，而是一个派生层。每个时间点记录来源。

> **空间选择规则（v1）**：当前采用全域统一选择，即每个 `(valid_time, variable)` 全系统选择一个 source。如果后续 CLDAS 仅覆盖中国区域需要与 GFS/IFS 空间混合，可升级为 `UNIQUE (valid_time, variable, domain_id)` 或 grid-cell 级 lineage。

```json
{
  "valid_time": "2026-05-01T00:00:00Z",
  "variable": "prcp",
  "selected_source": "CLDAS",
  "fallback_order": ["CLDAS", "ERA5", "GFS", "IFS"],
  "source_cycle_time": "2026-04-30T00:00:00Z",
  "quality_flag": "best_available_realtime"
}
```

### 9.1 Best available 时间窗策略

不同时间窗口的数据源优先级不同：

| 时间窗口 | 优先源 | 备选 | 说明 |
|---|---|---|---|
| 过去 0–1 天 | CLDAS（若可用） | GDAS / GFS analysis / short forecast | 近实时，CLDAS 分辨率最高 |
| 过去 1–5 天 | CLDAS | ERA5（若已可用）、GDAS / GFS 补齐 | ERA5 约 5 天迟滞，此窗口内可能不可用 |
| 过去 5 天以前 | ERA5 / ERA5 final | CLDAS 历史 | ERA5 已完成 QC，质量最高 |
| 未来 0–7 天 | GFS scenario / IFS scenario | — | 各 scenario 独立保存，不合并 |

`best_available` 产品的 `selected_source` 字段记录实际选中的数据源，`fallback_order` 记录当时的优先级链路。CLDAS 权限未解决期间，过去 0–5 天降级为 ERA5 + GDAS/GFS 组合。

## 10. 前端时间列表

后端每个图层返回：

```json
{
  "layer_id": "met_prcp_gfs_2026043000",
  "native_time_resolution": "PT3H",
  "valid_times": [
    "2026-04-30T00:00:00Z",
    "2026-04-30T03:00:00Z",
    "2026-04-30T06:00:00Z"
  ]
}
```

前端时间滑块只在 `valid_times[]` 上移动，不自行推断缺失时刻。

## 11. Analysis + Forecast 曲线拼接

```text
past_segment:
  scenario = analysis_true_field
  time_range = [issue_time - 7d, issue_time]

future_segment_gfs:
  scenario = forecast_gfs_deterministic
  time_range = [issue_time, issue_time + 7d]

future_segment_ifs:
  scenario = forecast_ifs_deterministic
  time_range = [issue_time, min(issue_time + 7d, ifs_available_end)]
```

前端必须显示资料来源和起报时间。
